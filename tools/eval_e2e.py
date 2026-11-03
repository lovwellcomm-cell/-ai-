"""端到端对比：旧流程（识别目标 + 改写检索词 → 检索 → 生成 → 扩展）vs 新流程（规划器 → 检索 → 生成 → 复核∥扩展 → 最多回退一次）。

用法：python -m tools.eval_e2e [--engines old,new] [--only 1,5] [--tag 名字]
     回答改版对比：--engines new,plain（plain = 新流程 + 通俗版提示词，new 是专业版）
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
import json
import re
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
     "qpl24": "f0fd7be994bcc4b1", "qpl26h1": "329e3ba64f8cb0ec", "sd25": "c4b7d146264a84a8",
     "jd26h1": "287df54da8d255e6", "jd25q3": "ccb04b65494ef2fd", "qpl26q3": "58b83e17d89ae44d"}

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
    # 跨报告（事实笔记第二期）：cross=True 要求回答引用了先读那份报告的笔记片段；doc 可以是几份里任一份
    dict(n=16, cur="jd25", q="对比一下精达 2025 年报和 2026 三季报的营收和归母净利润",
         doc=["jd26q3", "jd25"], nums=["246.74", "234.31"], cross=True),
    dict(n=17, cur="jd25", q="精达近三年发展态势和最新季报比怎么样",
         doc=["jd26q3", "jd25"], nums=["246.74", "234.31"], cross=True),
    dict(n=18, cur="jd26q3", q="今年前三季度和去年全年比，营收增长快了吗",
         doc=["jd26q3", "jd25"], nums=["234.31", "246.74"], cross=True),
    dict(n=19, cur="qpl25", q="七匹狼 2025 年报和 2026 半年报的经营现金流对比",
         doc=["qpl26h1", "qpl25"], nums=[], cross=True),
    dict(n=20, cur="jd26q1", q="一季度和三季度的营收分别是多少",
         doc=["jd26q1", "jd26q3"], nums=["234.31"], cross=True),
    # 第三期。chain：先完整跑一遍前一问（记下笔记），再问这一问 —— 用户最早的原例子
    dict(n=21, cur="jd25", chain="精达近三年发展态势如何", q="结合最新季报对比一下",
         doc=["jd26q3", "jd25"], nums=["234.31"], cross=True),
    # 毛利率变化的原因写在半年报里：复核应要求到半年报补检索（judge 不强求，逐题表里看「补检索」）
    dict(n=22, cur="jd26q3", q="结合半年报看前三季度毛利率变化", doc=["jd26q3", "jd26h1"], nums=[], cross=True),
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


def run(case: dict, engine: str, prior: list[dict] | None = None) -> dict:
    """prior：前几轮的历史条目（带事实笔记），链式题（chain）用 —— 和 app.py 的 history 同一个形状。"""
    # plain：新流程，只是回答用通俗版提示词（rag/generator.PLAIN_RULE_8）
    style = "plain" if engine == "plain" else "pro"
    engine = "new" if engine == "plain" else engine
    from rag import caliber, config, planner, reviewer
    from ui.memory import recent_context
    from rag.company import extract_target
    from rag.generator import analyze, generate, get_client
    from rag.indexer import list_documents, load_index
    from rag.periods import period_of
    from rag.retriever import Retriever
    from rag import notes
    from rag.fidelity import unverified_in_tables
    from rag.financials import load_or_build as load_fin
    from rag.periods import report_label
    from ui.turn import plan_for, plan_resolution, step, with_period

    client = Counting(get_client())
    docs = list_documents(None)
    cur = _meta(case["cur"])
    prior = prior or []
    # 检索侧只用问题；规划器和生成侧用带结论摘要、笔记出处的上下文（和 app.py 一样）
    history = case.get("history") or [e["q"] for e in prior]
    ctx = recent_context(prior, config.FOLLOWUP_HISTORY_TURNS) if prior else history
    q = case["q"]
    t0 = time.time()
    kw = dict(current_company=cur.get("company", ""), current_year=cur.get("year", ""),
              current_period=period_of(cur), docs=docs, today=TODAY)

    def resolve(hint=""):
        if engine == "new":
            u = planner.understand(q, client=client, history=ctx, current=cur, docs=docs,
                                   today=TODAY, hint=hint)
            if u is None:
                u = planner.fallback(q, client=client, history=ctx, current=cur, today=TODAY)
            return u, plan_for(u, **kw)
        tgt = with_period(extract_target(q, client=client, current_company=cur.get("company", "")),
                          q, TODAY)
        return None, plan_resolution(tgt, **kw)

    u, plan = resolve()
    out = {"engine": "plain" if style == "plain" else engine, "n": case["n"]}
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
    company, year, period = meta.get("company", ""), meta.get("year", ""), period_of(meta)
    # 第二期：先读别的报告记笔记（只用本地已有的报告，和 app.py 的 run_prestep 同一个 notes.read_step）
    step_notes, steps = [], []
    for x in (u or {}).get("also_read") or []:
        p2 = plan_resolution({"company": company, "year": x["year"], "period": x["period"]},
                             current_company=company, current_year=year, current_period=period, docs=docs)
        if p2["action"] != "switch":
            steps.append((x["year"], x["period"], p2["action"]))
            continue
        sdoc = p2["doc"]["doc_id"]
        if notes.already_noted(prior, sdoc, x["need"]):
            steps.append((x["year"], x["period"], "have"))
            continue
        src = f"{company} {report_label(x['year'], x['period'])}"
        _, c2, e2 = load_index(sdoc)
        r2 = Retriever(c2, e2, company=company, year=str(x["year"]))
        got_notes = notes.read_step(
            sdoc, src, notes.prestep_question(company, report_label(x["year"], x["period"]), x["need"]),
            x["need"], fin=load_fin(sdoc),
            search=lambda qq, r2=r2: r2.search_with_queries(qq, client=client, use_llm=False)[0],
            gen=lambda qq, hh, x=x: "".join(generate(qq, hh, client=client, company=company,
                                                     year=str(x["year"]), period=x["period"],
                                                     focus=notes.PRESTEP_FOCUS, style="pro")),
            check=unverified_in_tables)
        step_notes += got_notes
        steps.append((x["year"], x["period"], len(got_notes)))
    n_retrieved = len(hits)
    hits = hits + notes.as_hits(prior, meta["doc_id"], extra=step_notes)
    state = {"turn": {"q": q, "hits": hits, "queries": qs, "status": "generating", "started": t0}}
    # 分步回答：回退的时间预算按主步骤估（rag/reviewer.decide），和 app.py 先读完才起算一致
    if (u or {}).get("also_read"):
        state["turn"]["main_started"] = time.time()
    src_main = f"{company} {report_label(year, period)}".strip()
    fin_main = load_fin(meta["doc_id"])
    others = notes.other_reports(docs, meta, meta["doc_id"])
    supplemented: list[str] = []
    # 历史条目里记笔记要的几个字段（ui/turn.history_entry）：链式题的下一问靠它们
    state["turn"].update(doc_id=meta["doc_id"], src_label=src_main, step_notes=step_notes,
                         fin_notes=notes.from_financials(fin_main, meta["doc_id"], src_main))

    def gen(qq, hh, *, client, company, year, history):
        t = state["turn"]
        focus = planner.focus_block(u) + reviewer.feedback_block(t.get("review_last"))
        return "".join(generate(qq, hh, client=client, company=company, year=year,
                                history=history, period=period, focus=focus, style=style))

    def ana(qq, hh, first, *, client, company, year, history):
        diag: dict = {}                                  # 和 app.py 的 _ana 同一个契约：(正文, 诊断)
        ext = analyze(qq, hh, first, client=client, company=company, year=year,
                      diag=diag, history=history, period=period)
        return ext, diag

    def facts_of(hh):
        return caliber.facts(notes.from_financials(fin_main, meta["doc_id"], src_main) + caliber.note_lines(hh))

    def rev(qq, answer, hh, *, client):
        return reviewer.review(qq, answer, hh, client=client, plan=u, facts=facts_of(hh),
                               reports=list(others))

    entry = None
    for _ in range(12):
        new, entry, err = step(state["turn"], gen=gen, ana=ana, client=client, company=company,
                               year=year, recent=ctx or None,
                               rev=rev if engine == "new" else None)
        if err:
            out.update(error=err)
            break
        if entry is not None:
            break
        if new["status"] == "retrieving" and others.get(new.get("retry_report") or ""):
            # 第三期：去别的报告补检索，和 app.py 的 supplement_hits 一样接原文片段，主报告片段照留
            sdoc = others[new["retry_report"]]
            _, c3, e3 = load_index(sdoc)
            r3 = Retriever(c3, e3, company=company, year=new["retry_report"][:4])
            found, used = r3.search_with_queries(q, client=client, use_llm=False,
                                                 queries=new.pop("retry_queries", None) or None)
            extra = notes.as_supplement(found, sdoc, f"{company} {new['retry_report']}",
                                        config.RETRY_OTHER_TOP_K)
            supplemented.append(new["retry_report"])
            new = dict(new, hits=[h for h in new["hits"] if not h.get("extra")] + extra,
                       queries=list(dict.fromkeys(list(new.get("queries") or []) + list(used))),
                       status="generating")
        elif new["status"] == "retrieving":
            hits, qs = retrieve(new.pop("retry_queries", None))
            n_retrieved = len(hits)
            hits = hits + notes.as_hits(prior, meta["doc_id"], extra=step_notes)
            new = dict(new, hits=hits, queries=qs, status="generating")
        elif new["status"] == "resolving":
            # 复核要求重新理解：评测里不换报告（换了也是本地切换），只重做规划和检索
            u, _ = resolve(new.get("replan_hint", ""))
            hits, qs = retrieve()
            n_retrieved = len(hits)
            hits = hits + notes.as_hits(prior, meta["doc_id"], extra=step_notes)
            new = dict(new, hits=hits, queries=qs, status="generating")
        state["turn"] = new
    final_hits = state["turn"].get("hits") or []
    answer = (entry or {}).get("a", "")
    out.update(action=plan["action"], doc=meta["doc_id"], answer=answer,
               hits=final_hits, review=(entry or {}).get("review"),
               steps=steps, n_retrieved=n_retrieved, supplemented=supplemented,
               # 交给用户的回答里还剩几处一定错的跨口径计算（rag/caliber.py）
               cross_span=sum(c["hard"] for c in caliber.cross_span(answer, final_hits, facts_of(final_hits))),
               notes=(entry or {}).get("notes") or [], src=src_main,
               secs=round(time.time() - t0, 1), calls=client.calls)
    return out


def judge(case: dict, got: dict) -> dict:
    from rag.fidelity import check

    if case.get("behave"):
        want = {"refuse": "out_of_scope", "clarify": "clarify"}[case["behave"]]
        return {"ok": got.get("action") == want, "doc": None, "nums": None, "abstain": None, "orphan": None,
                "cross": None}
    ans = got.get("answer") or ""
    want_docs = case["doc"] if isinstance(case["doc"], list) else [case["doc"]]
    doc_ok = got.get("doc") in [D[x] for x in want_docs]
    nums_ok = all(x in ans for x in case["nums"])
    abstain = ans.strip().startswith("年报中未找到") and bool(case["nums"])
    orphan = check(ans, got.get("hits") or []).get("无出处", 0) if ans else None
    # 跨报告：回答引用了笔记片段（编号排在检索片段之后的那几个）
    from rag.generator import cited_numbers
    hits = got.get("hits") or []
    cross = None
    if case.get("cross"):
        k = got.get("n_retrieved", len(hits))
        cross = any(n > k and hits[n - 1].get("note") for n in cited_numbers(ans, len(hits)))
    span = got.get("cross_span") or 0
    return {"ok": doc_ok and nums_ok and not abstain and not got.get("error") and cross is not False
            and not span,
            "doc": doc_ok, "nums": nums_ok, "abstain": abstain, "orphan": orphan, "cross": cross,
            "span": span, **readability(ans)}


_SENT_END = re.compile(r"(?<=[。！？])")
_FIXED = ("据片段数字计算", "据片段数据推断")


def readability(ans: str) -> dict:
    """可读性的几个粗指标（只看首轮回答，扩展分析不算）：
    · lead_num：第一句里有没有数字（结论先行、带关键数字）；
    · lead_len：第一句多少字；avg_len：平均句长；
    · jargon：「片段」这类内部用语出现几次（固定短语不算，界面会处理）。"""
    body = (ans or "").split("**扩展分析**")[0]
    prose = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("|"))
    sents = [x.strip() for x in _SENT_END.split(prose.replace("\n", "")) if x.strip()]
    first = sents[0] if sents else ""
    rest = prose
    for f in _FIXED:
        rest = rest.replace(f, "")
    return {"lead_num": bool(re.search(r"\d", first)), "lead_len": len(first),
            "avg_len": round(sum(map(len, sents)) / len(sents), 1) if sents else 0,
            "jargon": rest.count("片段")}


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
            prior, this = [], c
            if c.get("chain"):
                # 链式题：先完整跑前一问，把它的历史条目（含笔记）带进这一问；当前报告换成前一问用的那份
                first = run(dict(c, q=c["chain"]), e)
                prior = [{"q": c["chain"], "a": first.get("answer", ""), "notes": first.get("notes") or []}]
                this = dict(c, cur=next((k for k, v in D.items() if v == first.get("doc")), c["cur"]))
            got = run(this, e, prior=prior)
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
        ("跨报告引用了笔记", lambda e: f"{len(col(e, lambda r: r[3].get('cross')))} / {len(col(e, lambda r: r[3].get('cross') is not None))}"),
        ("跨口径计算（程序查出，合计）", lambda e: str(sum(r[3].get('span') or 0 for r in col(e, lambda r: True)))),
        ("无出处数字（合计）", lambda e: str(sum(r[3]['orphan'] or 0 for r in col(e, lambda r: True)))),
        ("耗时中位数（秒）", lambda e: str(med([r[2].get('secs', 0) for r in col(e, lambda r: True)]))),
        ("耗时最长（秒）", lambda e: str(max([r[2].get('secs', 0) for r in col(e, lambda r: True)] or [0]))),
        ("模型调用（合计）", lambda e: str(sum(r[2].get('calls', 0) for r in col(e, lambda r: True)))),
        ("首句带数字", lambda e: f"{len(col(e, lambda r: r[3].get('lead_num')))} / {len(col(e, lambda r: 'lead_num' in r[3]))}"),
        ("首句字数中位数", lambda e: str(med([r[3]['lead_len'] for r in col(e, lambda r: 'lead_len' in r[3])]))),
        ("平均句长中位数", lambda e: str(med([r[3]['avg_len'] for r in col(e, lambda r: 'avg_len' in r[3])]))),
        ("「片段」用语（合计）", lambda e: str(sum(r[3].get('jargon', 0) for r in col(e, lambda r: True)))),
    ]:
        lines.append(f"| {name} | " + " | ".join(fn(e) for e in engines) + " |")
    for eng in [e for e in engines if e in ("new", "plain")]:
        revs = [r[2].get("review") or {} for r in rows if r[1] == eng]
        lines.append(f"\n复核（{eng}）：通过 {sum(v.get('verdict') == 'pass' for v in revs)}，"
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
            if j.get("cross") is False:
                bits.append("没引用笔记")
            if g.get("steps"):
                bits.append("先读" + "、".join(f"{y}{p}:{n}" for y, p, n in g["steps"]))
            if g.get("supplemented"):
                bits.append("补检索→" + "、".join(g["supplemented"]))
            if j.get("span"):
                bits.append(f"跨口径{j['span']}")
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
    # 回答原文另存一份，改版对比要逐题读
    (OUT / f"e2e-{args.tag}.json").write_text(json.dumps(
        [{"n": c["n"], "q": c["q"], "engine": e, "answer": g.get("answer"), "ok": j["ok"],
          "orphan": j["orphan"], "review": (g.get("review") or {}).get("verdict"), "secs": g.get("secs")}
         for c, e, g, j in rows], ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
