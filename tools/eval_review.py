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
"""
from __future__ import annotations

import argparse
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
_AMOUNT_RE = re.compile(r"(\d[\d,]*\.\d{2})(\s*亿元)")


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

    from rag import planner, reviewer
    from rag.generator import generate, get_client
    from rag.indexer import load_index
    from rag.retriever import Retriever

    client = get_client()

    def answer_one(item):
        doc_id, q = item
        meta, chunks, emb = load_index(doc_id)
        r = Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))
        plan = planner.understand(q, client=client, current=meta, docs=[meta])
        hits, _ = r.search_with_queries(q, client=client, use_llm=True,
                                        queries=(plan or {}).get("queries") or None,
                                        restated=(plan or {}).get("restated", ""))
        text = "".join(generate(q, hits, client=client, company=meta.get("company", ""),
                                year=meta.get("year", ""), focus=planner.focus_block(plan)))
        return {"q": q, "doc": meta["name"], "year": meta.get("year", ""), "plan": plan,
                "hits": hits, "answer": text}

    with ThreadPoolExecutor(6) as pool:
        goods = list(pool.map(answer_one, QUESTIONS))

    cases = []
    for i, g in enumerate(goods):
        cases.append(dict(g, kind="好回答", expect="pass", text=g["answer"]))
        other = goods[(i + 1) % len(goods)]["answer"]
        for kind in ("改数字", "编原因", "错年份", "答非所问"):
            bad = inject(kind, g["answer"], other, g["year"])
            if bad and bad != g["answer"]:
                cases.append(dict(g, kind=kind, expect="block", text=bad))

    def review_one(c):
        res = reviewer.review(c["q"], c["text"], c["hits"], client=client, plan=c["plan"])
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
    print(md)


if __name__ == "__main__":
    main()
