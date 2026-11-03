"""复核器评测：真实回答该放行，注入了错误的回答该拦下。

用法：python -m tools.eval_review [--tag 名字]　结果写到 eval_out/review-<tag>.md（不入库）。要真实模型。

做法沿用 tests/test_eval_rubric.py 的三类用例：
  · 好回答（误报护栏）：真实流程（规划器 → 检索 → 生成）产出的回答。复核**不该**拦。
    被拦的逐条列出理由 —— 有可能回答真有问题，要人看。
  · 注入错误（必须响）：在好回答上做四种改动，复核**必须**拦。
      改数字   把回答里第一个金额改大 40
      编原因   末尾加一句片段里没有的归因
      错年份   把报告年份全换成早两年
      答非所问 换成另一道题的回答
      跨口径   （只对跨报告题）加一句「前三季度营收较去年全年下降 x%」—— 九个月比十二个月（第三期）

跨报告题：主报告检索片段 + 另一份报告的主要会计数据笔记（rag/notes.as_hits），复核时带上带口径的数
（rag/caliber.facts），和 app.py 一样。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).resolve().parent.parent / "eval_out"
JD, QPL = "2d3dee37263a663e", "6da0b63132e3f0f6"       # 精达股份 2025 年报、七匹狼 2025 年报
QUESTIONS = [
    (JD, "营业收入和归母净利润分别是多少？"),
    (JD, "研发投入多少，占营收比例是多少？"),
    (JD, "前五大客户销售额占比多少？"),
    (JD, "经营活动现金流为什么大幅增加？"),
    (QPL, "毛利率是多少，同比怎么变化？"),
    (QPL, "2025年的利润分配方案是什么？"),
]
JD26Q3, QPL26Q3 = "705059e66541f245", "58b83e17d89ae44d"
# 跨报告题：(主报告, 笔记来自哪份, 问题)
CROSS = [
    (JD26Q3, JD, "对比一下 2025 年报和 2026 三季报的营业收入和归母净利润"),
    (QPL26Q3, QPL, "七匹狼 2025 年报和 2026 三季报的营收、净利润对比"),
]
_AMOUNT_RE = re.compile(r"(\d[\d,]*\.\d{2})(\s*亿元)")


def cross_line(fact_list: list[dict]) -> str | None:
    """跨口径注入：用笔记里的全年营收和主报告的前三季度营收算一个「同比」—— 每个数都有出处，只是口径不同。"""
    rev = [f for f in fact_list if f["key"] == "revenue"]
    fy = max((f for f in rev if f["span"] == "FY"), key=lambda f: f["year"] or "", default=None)
    ytd = max((f for f in rev if f["span"] not in ("FY", "单季")), key=lambda f: f["year"] or "", default=None)
    if not fy or not ytd:
        return None
    pct = (ytd["value"] - fy["value"]) / fy["value"] * 100
    return (f"\n\n- 前三季度营业收入较 {fy['year']} 年全年{'下降' if pct < 0 else '增长'} "
            f"{abs(pct):.2f}%，增长明显放缓。")


def inject(kind: str, answer: str, other: str, year: str) -> str | None:
    if kind == "改数字":
        m = _AMOUNT_RE.search(answer)
        if not m:
            return None
        bumped = f"{float(m.group(1).replace(',', '')) + 40:.2f}"
        return answer[:m.start(1)] + bumped + answer[m.end(1):]
    if kind == "编原因":
        return answer.rstrip() + "\n\n- 主要原因是公司对核心产品提价约 15%，并获得政府专项补贴 3.20 亿元[1]。"
    if kind == "错年份":
        y = int(year)
        return answer.replace(str(y), str(y - 2)).replace(str(y - 1), str(y - 3))
    if kind == "答非所问":
        return other
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=datetime.now().strftime("%m%d-%H%M"))
    args = ap.parse_args()

    from rag import caliber, notes, planner, reviewer
    from rag.financials import load_or_build as load_fin
    from rag.generator import generate, get_client
    from rag.indexer import list_documents, load_index
    from rag.periods import period_of, report_label
    from rag.retriever import Retriever

    client = get_client()
    docs = {d["doc_id"]: d for d in list_documents(None)}

    def label(meta):
        return f"{meta.get('company', '')} {report_label(meta.get('year', ''), period_of(meta))}".strip()

    def answer_one(item):
        doc_id, note_doc, q = item if len(item) == 3 else (item[0], None, item[1])
        meta, chunks, emb = load_index(doc_id)
        r = Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))
        plan = planner.understand(q, client=client, current=meta, docs=[meta])
        hits, _ = r.search_with_queries(q, client=client, use_llm=True,
                                        queries=(plan or {}).get("queries") or None,
                                        restated=(plan or {}).get("restated", ""))
        main_notes = notes.from_financials(load_fin(doc_id), doc_id, label(meta))
        if note_doc:
            got = notes.from_financials(load_fin(note_doc), note_doc, label(docs[note_doc]))
            hits = hits + notes.as_hits([], doc_id, extra=got)
        text = "".join(generate(q, hits, client=client, company=meta.get("company", ""),
                                year=meta.get("year", ""), period=period_of(meta),
                                focus=planner.focus_block(plan)))
        return {"q": q, "doc": meta["name"], "year": meta.get("year", ""), "plan": plan,
                "hits": hits, "answer": text, "cross": bool(note_doc),
                "facts": caliber.facts(main_notes + caliber.note_lines(hits))}

    with ThreadPoolExecutor(6) as pool:
        goods = list(pool.map(answer_one, QUESTIONS + CROSS))

    cases = []
    for i, g in enumerate(goods):
        cases.append(dict(g, kind="好回答", expect="pass", text=g["answer"]))
        other = goods[(i + 1) % len(goods)]["answer"]
        for kind in ("改数字", "编原因", "错年份", "答非所问"):
            bad = inject(kind, g["answer"], other, g["year"])
            if bad and bad != g["answer"]:
                cases.append(dict(g, kind=kind, expect="block", text=bad))
        if g["cross"] and (line := cross_line(g["facts"])):
            cases.append(dict(g, kind="跨口径", expect="block", text=g["answer"].rstrip() + line))

    def review_one(c):
        res = reviewer.review(c["q"], c["text"], c["hits"], client=client, plan=c["plan"],
                              facts=c["facts"])
        return dict(c, res=res)

    with ThreadPoolExecutor(6) as pool:
        done = list(pool.map(review_one, cases))

    lines = [f"# 复核器评测 · {datetime.now():%Y-%m-%d %H:%M}", ""]
    kinds: dict[str, list] = {}
    for c in done:
        v = (c["res"] or {}).get("verdict", "error")
        ok = (v == "pass") if c["expect"] == "pass" else (v not in ("pass", "error"))
        c["ok"] = ok
        kinds.setdefault(c["kind"], []).append(ok)
    lines += ["| 类型 | 期望 | 判对 |", "|---|---|---|"]
    for k, oks in kinds.items():
        lines.append(f"| {k} | {'放行' if k == '好回答' else '拦下'} | {sum(oks)} / {len(oks)} |")
    lines += ["", "## 判错的", ""]
    for c in done:
        if c["ok"]:
            continue
        res = c["res"] or {}
        lines.append(f"### [{c['kind']}] {c['doc']}「{c['q']}」→ {res.get('verdict', 'error')}")
        lines.append(f"- 复核说：{res.get('note', '')}；没答到：{[p['point'] for p in res.get('points', []) if not p['ok']]}；"
                     f"找不到依据：{res.get('unsupported', [])}")
        lines.append(f"- 回答开头：{c['text'][:160].replace(chr(10), ' ')}")
        lines.append("")
    lines += ["## 拦下注入错误时给的理由（抽查）", ""]
    for c in done:
        if c["ok"] and c["expect"] == "block":
            lines.append(f"- [{c['kind']}]「{c['q']}」→ {c['res']['verdict']}：{c['res'].get('note', '')}")
    md = "\n".join(lines)
    OUT.mkdir(exist_ok=True)
    (OUT / f"review-{args.tag}.md").write_text(md, encoding="utf-8")
    # 评测页用（tools/build_eval_snapshot.py）：每个用例的类型、判定、理由，好回答原文和注入后的版本
    (OUT / f"review-{args.tag}.json").write_text(json.dumps(
        [{"q": c["q"], "doc": c["doc"], "kind": c["kind"], "expect": c["expect"], "ok": c["ok"],
          "verdict": (c["res"] or {}).get("verdict", "error"), "note": (c["res"] or {}).get("note", ""),
          "answer": c["answer"], "text": c["text"]} for c in done],
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
