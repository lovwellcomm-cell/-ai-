"""「理解问题」评测：tools/intent_cases.py 里的每道题，系统理解得对不对。

用法：python -m tools.eval_intent [--engine baseline] [--only trend-1,follow-2] [--tag 名字]
结果写到 eval_out/intent-<engine>-<tag>.md / .json。要真实模型（.env 里的 DEEPSEEK_API_KEY）。

只比对「理解」这一层：切到哪份报告、按什么意图处理、出不出卡、检索词覆盖了没有。
不跑检索和生成 —— 那是端到端评测（tools/eval_rubric.py）的事。

engine：
  baseline  现有流程：识别公司年份（rag/company.py，模型）+ 报告期规则 + 选文档 +
            趋势/行业/出卡规则 + 改写检索词（rag/retriever.py，模型）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.intent_cases import CASES, DOCS, TODAY  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "eval_out"


def _doc(doc_id: str) -> dict:
    return next(d for d in DOCS if d["doc_id"] == doc_id)


def _plan_key(plan: dict) -> tuple | str:
    """plan_resolution 的结果 → 和测试集一样的写法，方便比对和打印。"""
    a = plan.get("action")
    if a == "switch":
        return ("switch", plan["doc"]["doc_id"])
    if a == "import":
        return ("import", plan["company"], plan["year"], plan["period"])
    return a


def run_baseline(case: dict, client) -> dict:
    from rag.company import extract_target
    from rag.industry import wants_industry
    from rag.history import wants_trend
    from rag.periods import period_of
    from rag.retriever import llm_rewrite, rule_rewrite
    from ui.card import wants_card
    from ui.turn import plan_resolution, with_period

    today = date(*TODAY)
    cur = _doc(case.get("cur", "jd25"))
    history = case.get("history") or []
    q = case["q"]
    t0 = time.time()
    tgt = extract_target(q, client=client, current_company=cur["company"])
    tgt = with_period(tgt, q, today)
    plan = plan_resolution(tgt, current_company=cur["company"], current_year=cur["year"],
                           current_period=period_of(cur), docs=DOCS, today=today)
    t1 = time.time()
    # 检索词和真实检索一致：规则改写一条 + 模型改写；追问时只留模型给的（rag/retriever.py:_expand）
    doc = plan["doc"] if plan.get("action") == "switch" else cur
    diag: dict = {}
    extra = llm_rewrite(q, doc["year"], client=client, history=history or None, diag=diag)
    queries = [] if (extra and history) else [rule_rewrite(q, doc["company"], doc["year"])]
    queries = list(dict.fromkeys(x for x in queries + extra if x))
    card = wants_card(q, previous=history)
    if wants_industry(q):
        intent = "industry"
    elif card and wants_trend(q, previous=history):
        intent = "trend"
    else:
        intent = "report_qa"
    return {"plan": _plan_key(plan), "intent": intent, "card": card, "queries": queries,
            "target": {k: tgt.get(k) for k in ("company", "year", "period", "multiple")},
            "rewrite": diag.get("rewrite", ""),
            "secs": round(time.time() - t0, 1), "secs_target": round(t1 - t0, 1), "calls": 2}


_INTENT = {"report": "report_qa"}


def run_planner(case: dict, client) -> dict:
    """新流程：rag/planner.py 一次调用 + 规则兜底 → ui/turn.plan_for 选报告。
    规划器失败时和线上一样退回旧流程（记 source=fallback），检索词再走旧的改写。"""
    from rag import planner
    from rag.periods import period_of
    from rag.retriever import llm_rewrite, rule_rewrite
    from ui.turn import plan_for

    today = date(*TODAY)
    cur = _doc(case.get("cur", "jd25"))
    history = case.get("history") or []
    q = case["q"]
    t0 = time.time()
    u = planner.understand(q, client=client, history=history, current=cur, docs=DOCS, today=today)
    calls = 1
    if u is None:
        u = planner.fallback(q, client=client, history=history, current=cur, today=today)
        calls += 1
    plan = plan_for(u, current_company=cur["company"], current_year=cur["year"],
                    current_period=period_of(cur), docs=DOCS, today=today)
    queries = u["queries"]
    if not queries and u["intent"] in ("report", "trend"):
        doc = plan["doc"] if plan.get("action") == "switch" else cur
        extra = llm_rewrite(q, doc["year"], client=client, history=history or None)
        queries = [] if (extra and history) else [rule_rewrite(q, doc["company"], doc["year"])]
        queries = list(dict.fromkeys(x for x in queries + extra if x))
        calls += 1
    key = _plan_key(plan)
    if key == "out_of_scope":
        key = "current"                      # 不换报告，和测试集的写法一致
    return {"plan": key, "intent": _INTENT.get(u["intent"], u["intent"]), "card": u["card"],
            "queries": queries, "source": u["source"], "restated": u.get("restated"),
            "subquestions": u.get("subquestions"), "clarify": u.get("clarify"),
            "reply": u.get("reply"),
            "target": {k: (u.get("target") or {}).get(k) for k in ("company", "year", "period", "multiple")},
            "secs": round(time.time() - t0, 1), "calls": calls}


ENGINES = {"baseline": run_baseline, "planner": run_planner}


def score(case: dict, got: dict) -> dict:
    """逐项比对。None 表示这一项这道题不计分。"""
    plans = [tuple(p) if isinstance(p, (list, tuple)) else p for p in case["plan"]]
    gp = got["plan"]
    gp = tuple(gp) if isinstance(gp, list) else gp
    res = {"plan": gp in plans, "intent": got["intent"] in case["intent"]}
    res["card"] = None if case.get("card") is None else (bool(got["card"]) == case["card"])
    anchors = case.get("anchors")
    if anchors is None:
        res["anchors"] = None
        res["missing"] = []
    else:
        text = " ".join(got.get("queries") or [])
        missing = [g for g in anchors if not any(w in text for w in g)]
        res["anchors"] = not missing
        res["missing"] = ["/".join(g) for g in missing]
    res["ok"] = all(v is not False for k, v in res.items() if k in ("plan", "intent", "card", "anchors"))
    return res


def _fmt(v) -> str:
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(str(x) for x in v) + ")"
    return str(v)


def report(engine: str, rows: list[dict]) -> str:
    fields = ("plan", "intent", "card", "anchors")
    lines = [f"# 理解问题评测 · {engine} · {datetime.now():%Y-%m-%d %H:%M}", ""]
    n_ok = sum(r["score"]["ok"] for r in rows)
    lines.append(f"**整题全对 {n_ok} / {len(rows)}**")
    lines.append("")
    lines.append("| 项 | 对 / 计分题数 |")
    lines.append("|---|---|")
    for f in fields:
        scored = [r for r in rows if r["score"][f] is not None]
        lines.append(f"| {f} | {sum(r['score'][f] for r in scored)} / {len(scored)} |")
    secs = sorted(r["got"]["secs"] for r in rows if "secs" in r["got"])
    if secs:
        lines.append(f"| 耗时中位数 | {secs[len(secs) // 2]} 秒（最慢 {secs[-1]} 秒） |")
    lines += ["", "## 按类别", "", "| 类别 | 全对 |", "|---|---|"]
    cats: dict[str, list] = {}
    for r in rows:
        cats.setdefault(r["case"]["cat"], []).append(r["score"]["ok"])
    for c, oks in cats.items():
        lines.append(f"| {c} | {sum(oks)} / {len(oks)} |")
    lines += ["", "## 错误清单", ""]
    for r in rows:
        if r["score"]["ok"]:
            continue
        c, g, s = r["case"], r["got"], r["score"]
        hist = f"（前文：{' → '.join(c['history'])}）" if c.get("history") else ""
        lines.append(f"### {c['id']}　「{c['q']}」{hist}")
        if s["plan"] is False:
            lines.append(f"- 动作：得到 `{_fmt(g['plan'])}`，期望 {' 或 '.join(f'`{_fmt(p)}`' for p in c['plan'])}"
                         f"（识别结果 {g.get('target')}）")
        if s["intent"] is False:
            lines.append(f"- 意图：得到 `{g['intent']}`，期望 {' 或 '.join(c['intent'])}")
        if s["card"] is False:
            lines.append(f"- 指标卡：得到 {'出' if g['card'] else '不出'}，期望 {'出' if c['card'] else '不出'}")
        if s["anchors"] is False:
            lines.append(f"- 检索词缺：{'、'.join(s['missing'])}　实际 {g['queries']}")
        if g.get("error"):
            lines.append(f"- 出错：{g['error']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="baseline", choices=sorted(ENGINES))
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default=datetime.now().strftime("%m%d-%H%M"))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    from rag.generator import get_client
    client = get_client()
    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    run = ENGINES[args.engine]

    def one(case):
        try:
            got = run(case, client)
        except Exception as e:                      # 一道题出错不影响其他题
            got = {"plan": None, "intent": None, "card": None, "queries": [],
                   "error": f"{type(e).__name__}: {e}"}
        return {"case": case, "got": got, "score": score(case, got)}

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(one, cases))
    OUT.mkdir(exist_ok=True)
    stem = OUT / f"intent-{args.engine}-{args.tag}"
    md = report(args.engine, rows)
    stem.with_suffix(".md").write_text(md, encoding="utf-8")
    stem.with_suffix(".json").write_text(
        json.dumps([{"id": r["case"]["id"], "got": r["got"], "score": r["score"]} for r in rows],
                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(md)
    print(f"\n写入 {stem}.md")


if __name__ == "__main__":
    main()
