"""端到端对比：旧流程（识别目标 + 改写检索词 → 检索 → 生成 → 扩展）vs 新流程（规划器 → 检索 → 生成 → 复核∥扩展 → 最多回退一次）。

用法：python -m tools.eval_e2e [--engines old,new] [--only 1,5] [--tag 名字]
结果写到 eval_out/e2e-<tag>.md（不入库）。要真实模型；只用本地已有的报告（不导入新报告，免得测的是网速）。

驱动的是线上同一个状态机 ui/turn.step，只是不经过 Streamlit。联网补充（web）两边都关掉 ——
它和这次改动无关，开着只会让耗时多一大截噪声。

评判**不用复核器**（否则新流程自己考自己）：
  · 用对报告：答题用的是不是期望的那份；
  · 关键数字：期望的数字（取自年报原文，前几轮已核对）都出现在回答里；
  · 误答未找到：回答以「年报中未找到」开头，但期望里有数字；
  · 无出处数字：rag/fidelity.check 的「无出处」个数（会有合法换算被误算，两边同口径，只看相对）；
  · 行为题（天气、「对比一下」）：期望直接说明 / 反问，而不是硬检索作答。
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).resolve().parent.parent / "eval_out"
TODAY = date(2026, 11, 2)
D = {"jd25": "2d3dee37263a663e", "jd26q3": "705059e66541f245", "jd26q1": "e7c61d0ee9fdf92a",
     "mt25": "474905deeaf0f875", "mt24": "5299f4940e2ce4e9", "qpl25": "6da0b63132e3f0f6",
     "qpl24": "f0fd7be994bcc4b1", "qpl26h1": "329e3ba64f8cb0ec", "sd25": "c4b7d146264a84a8"}

CASES = [
    dict(n=1, cur="jd25", q="营业收入和归母净利润分别是多少？", doc="jd25", nums=["246.74", "5.99"]),
    dict(n=2, cur="jd25", q="研发投入多少？", doc="jd25", nums=["1.21"]),
    dict(n=3, cur="jd25", q="前五大客户销售额占比多少？", doc="jd25", nums=["9.69"]),
    dict(n=4, cur="jd25", q="那净利润呢", history=["2025年营业收入是多少？"], doc="jd25", nums=["5.99"]),
    dict(n=5, cur="jd25", q="茅台2024年营收和净利润", doc="mt24", nums=["1708.99", "862.28"]),
    dict(n=6, cur="jd25", q="贵州茅台去年年报的净利润", doc="mt25", nums=["823.2"]),
    dict(n=7, cur="jd25", q="时代新材营收多少", doc="sd25", nums=["214.71"]),
    dict(n=8, cur="jd25", q="最新财报如何", doc="jd26q3", nums=["234.31"]),
    dict(n=9, cur="jd26q3", q="近几年发展如何", doc="jd25", nums=["246.74", "223.23"]),
    dict(n=10, cur="jd26q3", q="去年全年营收多少", doc="jd25", nums=["246.74"]),
    dict(n=11, cur="qpl25", q="分红方案是什么", doc="qpl25", nums=["10股"]),
    dict(n=12, cur="qpl25", q="毛利率是多少，同比怎么变化？", doc="qpl25", nums=[]),
    dict(n=13, cur="jd25", q="经营活动现金流为什么大幅增加？", doc="jd25", nums=["24.75"]),
    dict(n=14, cur="jd25", q="今天天气怎么样", behave="refuse"),
    dict(n=15, cur="jd25", q="对比一下", behave="clarify"),
]


class Counting:
    """包一层模型客户端，数 chat.completions.create 调了几次（线程安全够用：只做 +1）。"""

    def __init__(self, inner):
        self._inner, self.calls = inner, 0
        outer = self

        class _Comp:
            def create(self, **kw):
                outer.calls += 1
                return inner.chat.completions.create(**kw)

        class _Chat:
            completions = _Comp()

        self.chat = _Chat()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _meta(key: str) -> dict:
    from rag.indexer import list_documents
    return next(d for d in list_documents(None) if d["doc_id"] == D[key])


def run(case: dict, engine: str) -> dict:
    from rag import planner, reviewer
    from rag.company import extract_target
    from rag.generator import analyze, generate, get_client
    from rag.indexer import list_documents, load_index
    from rag.periods import period_of
    from rag.retriever import Retriever
    from ui.turn import plan_for, plan_resolution, step, with_period

    client = Counting(get_client())
    docs = list_documents(None)
    cur = _meta(case["cur"])
    history = case.get("history") or []
    q = case["q"]
    t0 = time.time()
    kw = dict(current_company=cur.get("company", ""), current_year=cur.get("year", ""),
              current_period=period_of(cur), docs=docs, today=TODAY)

    def resolve(hint=""):
        if engine == "new":
            u = planner.understand(q, client=client, history=history, current=cur, docs=docs,
                                   today=TODAY, hint=hint)
            if u is None:
                u = planner.fallback(q, client=client, history=history, current=cur, today=TODAY)
            return u, plan_for(u, **kw)
        tgt = with_period(extract_target(q, client=client, current_company=cur.get("company", "")),
                          q, TODAY)
        return None, plan_resolution(tgt, **kw)

    u, plan = resolve()
    out = {"engine": engine, "n": case["n"]}
    if plan["action"] in ("clarify", "out_of_scope", "multiple", "import"):
        out.update(action=plan["action"], doc=None, answer=(u or {}).get("reply") or "",
                   secs=round(time.time() - t0, 1), calls=client.calls)
        return out
    meta = plan["doc"] if plan["action"] == "switch" else cur
    _, chunks, emb = load_index(meta["doc_id"])
    r = Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))

    def retrieve(queries=None):
        return r.search_with_queries(q, client=client, use_llm=True, history=history or None,
                                     queries=queries or (u or {}).get("queries") or None,
                                     restated=(u or {}).get("restated", ""))

    hits, qs = retrieve()
    state = {"turn": {"q": q, "hits": hits, "queries": qs, "status": "generating", "started": t0}}
    company, year, period = meta.get("company", ""), meta.get("year", ""), period_of(meta)

    def gen(qq, hh, *, client, company, year, history):
        t = state["turn"]
        focus = planner.focus_block(u) + reviewer.feedback_block(t.get("review_last"))
        return "".join(generate(qq, hh, client=client, company=company, year=year,
                                history=history, period=period, focus=focus))

    def ana(qq, hh, first, *, client, company, year, history):
        diag: dict = {}                                  # 和 app.py 的 _ana 同一个契约：(正文, 诊断)
        ext = analyze(qq, hh, first, client=client, company=company, year=year,
                      diag=diag, history=history, period=period)
        return ext, diag

    def rev(qq, answer, hh, *, client):
        return reviewer.review(qq, answer, hh, client=client, plan=u)

    entry = None
    for _ in range(12):
        new, entry, err = step(state["turn"], gen=gen, ana=ana, client=client, company=company,
                               year=year, recent=history or None,
                               rev=rev if engine == "new" else None)
        if err:
            out.update(error=err)
            break
        if entry is not None:
            break
        if new["status"] == "retrieving":
            hits, qs = retrieve(new.pop("retry_queries", None))
            new = dict(new, hits=hits, queries=qs, status="generating")
        elif new["status"] == "resolving":
            # 复核要求重新理解：评测里不换报告（换了也是本地切换），只重做规划和检索
            u, _ = resolve(new.get("replan_hint", ""))
            hits, qs = retrieve()
            new = dict(new, hits=hits, queries=qs, status="generating")
        state["turn"] = new
    out.update(action=plan["action"], doc=meta["doc_id"], answer=(entry or {}).get("a", ""),
               hits=state["turn"].get("hits") or [], review=(entry or {}).get("review"),
               secs=round(time.time() - t0, 1), calls=client.calls)
    return out


def judge(case: dict, got: dict) -> dict:
    from rag.fidelity import check

    if case.get("behave"):
        want = {"refuse": "out_of_scope", "clarify": "clarify"}[case["behave"]]
        return {"ok": got.get("action") == want, "doc": None, "nums": None, "abstain": None, "orphan": None}
    ans = got.get("answer") or ""
    doc_ok = got.get("doc") == D[case["doc"]]
    nums_ok = all(x in ans for x in case["nums"])
    abstain = ans.strip().startswith("年报中未找到") and bool(case["nums"])
    orphan = check(ans, got.get("hits") or []).get("无出处", 0) if ans else None
    return {"ok": doc_ok and nums_ok and not abstain and not got.get("error"),
            "doc": doc_ok, "nums": nums_ok, "abstain": abstain, "orphan": orphan}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="old,new")
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default=datetime.now().strftime("%m%d-%H%M"))
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()
    engines = args.engines.split(",")
    cases = [c for c in CASES if not args.only or str(c["n"]) in args.only.split(",")]
    jobs = [(c, e) for c in cases for e in engines]

    def one(job):
        c, e = job
        try:
            got = run(c, e)
        except Exception as ex:
            got = {"engine": e, "n": c["n"], "error": f"{type(ex).__name__}: {ex}", "secs": 0, "calls": 0}
        return c, e, got, judge(c, got)

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(one, jobs))

    lines = [f"# 端到端对比 · {datetime.now():%Y-%m-%d %H:%M}", ""]
    lines += ["| 指标 | " + " | ".join(engines) + " |", "|---|" + "---|" * len(engines)]

    def col(e, f):
        return [r for r in rows if r[1] == e and f(r)]

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0

    for name, fn in [
        ("整题合格", lambda e: f"{len(col(e, lambda r: r[3]['ok']))} / {len(col(e, lambda r: True))}"),
        ("用对报告", lambda e: f"{len(col(e, lambda r: r[3]['doc']))} / {len(col(e, lambda r: r[3]['doc'] is not None))}"),
        ("关键数字都在", lambda e: f"{len(col(e, lambda r: r[3]['nums']))} / {len(col(e, lambda r: r[3]['nums'] is not None))}"),
        ("误答未找到", lambda e: str(len(col(e, lambda r: r[3]['abstain'])))),
        ("无出处数字（合计）", lambda e: str(sum(r[3]['orphan'] or 0 for r in col(e, lambda r: True)))),
        ("耗时中位数（秒）", lambda e: str(med([r[2].get('secs', 0) for r in col(e, lambda r: True)]))),
        ("耗时最长（秒）", lambda e: str(max([r[2].get('secs', 0) for r in col(e, lambda r: True)] or [0]))),
        ("模型调用（合计）", lambda e: str(sum(r[2].get('calls', 0) for r in col(e, lambda r: True)))),
    ]:
        lines.append(f"| {name} | " + " | ".join(fn(e) for e in engines) + " |")
    if "new" in engines:
        revs = [r[2].get("review") or {} for r in rows if r[1] == "new"]
        lines.append(f"\n复核：通过 {sum(v.get('verdict') == 'pass' for v in revs)}，"
                     f"未通过 {sum(v.get('verdict') == 'fail' for v in revs)}，"
                     f"出错 {sum(v.get('verdict') == 'error' for v in revs)}，"
                     f"回退过 {sum(1 for v in revs if v.get('rounds'))} 题")
    lines += ["", "## 逐题", "", "| # | 问题 | " + " | ".join(engines) + " |", "|---|---|" + "---|" * len(engines)]
    for c in cases:
        cells = []
        for e in engines:
            r = next(x for x in rows if x[0] is c and x[1] == e)
            g, j = r[2], r[3]
            if g.get("error"):
                cells.append("出错：" + g["error"][:60])
                continue
            mark = "✓" if j["ok"] else "✗"
            bits = [mark, f"{g.get('secs')}s", f"{g.get('calls')}次"]
            if j["doc"] is False:
                bits.append("报告错:" + next((k for k, v in D.items() if v == g.get("doc")), str(g.get("action"))))
            if j["nums"] is False:
                bits.append("缺数字")
            if j["abstain"]:
                bits.append("误答未找到")
            if c.get("behave"):
                bits.append(str(g.get("action")))
            rv = g.get("review") or {}
            if rv:
                bits.append(f"复核{rv.get('verdict')}" + ("·回退" if rv.get("rounds") else ""))
            cells.append(" ".join(bits))
        hist = f"（前文：{c['history'][0]}）" if c.get("history") else ""
        lines.append(f"| {c['n']} | {c['q']}{hist} @{c['cur']} | " + " | ".join(cells) + " |")
    md = "\n".join(lines)
    OUT.mkdir(exist_ok=True)
    (OUT / f"e2e-{args.tag}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
