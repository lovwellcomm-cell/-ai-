"""回合状态机的纯核心：生成 → 扩展 → 完成。

app.py 原本把「生成完转到哪、什么时候收尾、出错怎么办」全写在 Streamlit 脚本里。
那段逻辑要跨 rerun 传状态、中间调两次真实模型，**却一行自动化测试都没有** ——
grep `advance_turn` 在 tests/ 里没有任何结果。

把决策过程抽到这里之后，app.py 只负责接 session_state 和真实模型调用，
而"谁在什么时候转到哪个状态"可以用假的模型跑测试。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from rag.company import normalize, same_company
from rag.external import join_external, should_search
from rag.fidelity import unverified_in_tables
from rag.generator import join_answer, should_analyze
from rag.drivers import brief as drivers_brief
from rag.healthcheck import brief as health_brief, compact as compact_health
from rag import notes as fact_notes
from rag.reviewer import decide, summary
from rag.periods import (FY, RANK, latest_quarter, latest_report, latest_year, next_report,
                         period_end, period_in, period_of, relative_year, wants_latest,
                         wants_latest_quarter)
from ui.card import CARD_KEYS


def conversation_key(doc: dict) -> str:
    """一段对话属于谁：同一家公司的不同年度年报共用一段对话。

    「七匹狼 2024」和「七匹狼 2025」→ 同一个键，问完 2025 再问「那 2024 年呢」时
    对话接着往下走，而不是跳到另一段空白对话。公司名去掉「股份」「集团」等后缀再比，
    抽不出公司名的上传文档退回按文档 id 单独成段。

    这里刻意用**精确**的规范化名而不是 `same_company` 的模糊匹配：模糊匹配的结果
    依赖当时有哪些文档，新导入一份年报就可能让键变掉，整段对话凭空消失。
    """
    name = normalize(doc.get("company", ""))
    return f"co:{name}" if name else f"doc:{doc.get('doc_id', '')}"


def company_groups(docs) -> list[dict]:
    """侧边栏每家公司一行：[{"key", "label", "docs"}]，按每家公司第一次出现的顺序。

    分组键和对话分段用同一个（conversation_key），所以「点公司」和「这段对话」一一对应。
    抽不出公司名的上传文档各自成一行，显示文件名。
    """
    groups: dict[str, dict] = {}
    for d in docs:
        key = conversation_key(d)
        g = groups.setdefault(key, {"key": key, "label": d.get("company") or d.get("name", ""),
                                    "docs": []})
        g["docs"].append(d)
    return list(groups.values())


def pick_for_company(group_docs: list[dict], last_id: str | None = None) -> dict:
    """点公司时切到哪份：上次在这家公司看的那份；没有就最新一期（年份大，同年年报优先）。"""
    for d in group_docs:
        if d.get("doc_id") == last_id:
            return d
    return max(group_docs, key=lambda d: (str(d.get("year", "")), RANK[period_of(d)]))


def history_entry(turn: dict, answer: str) -> dict:
    """一轮完成时写进 history 的形状。

    `analyze` / `web` 只是排查用的状态（ok / no_add / ungrounded / error / skipped /
    aborted…），组件不直接读它们，但 `web_sources` 要交给组件渲染来源链接。
    失败原因一并留下 —— 只记一个 "error" 而不记为什么，等于把排查线索丢在回合对象里、
    回合一清就没了。
    """
    entry = {
        "q": turn["q"],
        "a": answer,
        "hits": turn["hits"],
        "queries": turn["queries"],
        "analyze": turn.get("analyze", ""),
        "web": turn.get("web", ""),
        "web_sources": list(turn.get("web_sources") or []),
    }
    if turn.get("analyze_error"):
        entry["analyze_error"] = turn["analyze_error"]
    if turn.get("web_error"):
        entry["web_error"] = turn["web_error"]
    # 行业动态回合：界面据此不再显示「行业动态」按钮、不出指标卡。
    # 反问（clarify）、超出范围（refuse）也是 kind：它们没检索，回答是规划器给的一句话。
    # 体检（health）同理：没检索，正文和卡片是程序写的。
    if turn.get("kind"):
        entry["kind"] = turn["kind"]
    # 反问的可选项：界面渲染成按钮，点了直接发送
    if turn.get("options"):
        entry["options"] = list(turn["options"])
    # 规划器怎么理解这个问题的（rag/planner.describe），检索轨迹里给人看
    if turn.get("understood"):
        entry["understood"] = turn["understood"]
    # 复核结论（rag/reviewer.summary）：界面据此标「已复核」或列出没通过的地方
    if turn.get("review"):
        entry["review"] = turn["review"]
    # 依据的是哪份年报：同一公司多个年度共用一段对话后，翻原页、标注年份都要靠它。
    if turn.get("doc_id"):
        entry["doc_id"] = turn["doc_id"]
    # 回答风格（通俗 / 专业）：界面据此决定要不要加术语解释、「算」标签、收起「为什么会这样」。
    # 只有年报回答才有（体检、几件大事是程序写的正文）；旧条目没有这个键，按专业版显示。
    if turn.get("style") and not turn.get("kind"):
        entry["style"] = turn["style"]
    # 这一轮用了多久（从发出问题到回答完整），界面在回答顶部显示「用时 n 秒」
    if turn.get("started"):
        entry["secs"] = max(0, int(round(time.time() - turn["started"])))
    # 自动导入的提示要跟着回答保留下来；没有就不写键，保持旧条目形状不变。
    if turn.get("notice"):
        entry["notice"] = turn["notice"]
    # 指标卡同理：只在回合上挂了卡时才写，否则回合一结束卡片就消失。
    for key in CARD_KEYS:
        if turn.get(key):
            entry[key] = turn[key]
    # 回答里有表格时逐格核对：找不到出处的数字交给界面用虚线标出来。
    # 在回合完成时算一次存下来，而不是每次 rerun 重算。
    check = unverified_in_tables(answer, turn.get("hits") or [])
    if check:
        entry["tableCheck"] = check
    # 事实笔记（rag/notes.py）：这一轮核对过的数字，带出处，后面几轮换了报告也能引用。
    # 只有年报回答才记（体检、几件大事、行业动态、反问没有核对过的表格）
    if not turn.get("kind"):
        found = fact_notes.merge(
            turn.get("fin_notes") or [],
            fact_notes.from_answer(answer, turn.get("hits") or [],
                                   (check or {}).get("unverified") or [],
                                   turn.get("doc_id", ""), turn.get("src_label", "")))
        if found:
            entry["notes"] = found
    return entry


def with_period(target: dict, question: str, today: date | None = None) -> dict:
    """在模型识别出的 {company, year} 上补「期」：问的是年报、一季报、半年报还是三季报。

    期用规则从问题里认，不交给模型：「前三季度」「上半年」「Q1」这些说法是封闭的，
    规则比模型稳，也省一次调用。「去年三季度」里的相对年份顺带换算（模型不换算相对年份）；
    「最新季报」没说哪一季 → 已过披露截止日的最近一期。
    「最新财报」→ 四种报告里已过截止日的最近一期，标上 latest；下一期如果报告期已经结束、
    只是还没到截止日（不少公司提前披露），一并记在 newer 里，导入时先试它。
    """
    out = dict(target)
    period = period_in(question)
    year = out.get("year")
    if year is None:
        year = relative_year(question, today)
    if period is None and wants_latest_quarter(question):
        latest_y, period = latest_quarter(today)
        year = year if year is not None else latest_y
    latest = wants_latest(question) if period is None and year is None else None
    if latest:
        today = today or date.today()
        year, period = (latest_year(FY, today), FY) if latest == FY else latest_report(today)
        newer = (year + 1, FY) if latest == FY else next_report(year, period)
        out["latest"] = latest
        if period_end(*newer) < today:
            out["newer"] = list(newer)
    out["year"] = year
    out["period"] = period
    return out


def plan_resolution(target: dict, *, current_company: str = "", current_year: str = "",
                    current_period: str = FY, docs=(), today: date | None = None) -> dict:
    """根据识别到的目标，决定这一轮用哪份文档。

    返回：
      {"action": "current"}                              用当前选中的文档
      {"action": "switch", "doc": meta}                  本地已有，直接切换
      {"action": "import", "company", "year", "period"}  本地没有，需要去巨潮下载
      {"action": "multiple"}                             一次问了多家，暂不支持

    target["period"] 为 None 表示没提哪一期：年份没变就留在当前文档（在三季报上问
    「营收怎么样」就答三季报），年份变了优先年报（「那 2024 年呢」→ 2024 年报）。

    纯函数：不读 session_state、不发请求，方便把“切错文档”这类错误挡在测试里。
    """
    company = (target.get("company") or "").strip()
    year = target.get("year")
    period = target.get("period")
    current_period = current_period or FY
    if target.get("multiple"):
        return {"action": "multiple"}
    same_year = year is None or str(year) == str(current_year)
    same_period = period is None or period == current_period
    if not company:
        # 「那2024年呢？」「一季度呢？」没有公司名，但年份或期变了 —— 按当前公司理解。
        # 「最新财报」也按当前公司理解：当前就是那一期时，本地仍可能有更新的一份。
        if current_company and (target.get("latest") or not (same_year and same_period)):
            company = current_company
        else:
            return {"action": "current"}
    here = same_company(company, current_company)
    if target.get("latest"):
        return _plan_latest(target, company, here, current_year, current_period, docs)
    if here and same_year and same_period:
        return {"action": "current"}
    if here and year is None and str(current_year).isdigit():
        # 在 2026 三季报上问「一季度呢」：同一年的一季报，而不是最新的一季报。
        year = int(current_year)

    candidates = [d for d in docs if same_company(d.get("company", ""), company)]
    if year is not None:
        candidates = [d for d in candidates if str(d.get("year", "")) == str(year)]
    if period is not None:
        candidates = [d for d in candidates if period_of(d) == period]
        if year is None:
            # 别的公司的「三季报」、没说年份：本地那份太旧就去巨潮拿最新的。
            floor = latest_year(period, today)
            candidates = [d for d in candidates
                          if str(d.get("year", "")).isdigit() and int(d["year"]) >= floor]
    if candidates:
        best = max(candidates, key=lambda d: (str(d.get("year", "")),
                                              RANK[period_of(d)],
                                              bool(d.get("demo"))))
        return {"action": "switch", "doc": best}
    return {"action": "import", "company": company, "year": year, "period": period or FY}


def quick_entry(turn: dict, text: str, kind: str, options: list[str] | None = None) -> dict:
    """不检索、不生成就收尾的一轮（反问、超出范围）。形状和 history_entry 一致。"""
    t = dict(turn, hits=[], queries=[], kind=kind)
    if options:
        t["options"] = options
    return history_entry(t, text)


def health_entry(turn: dict, hc: dict, label: str) -> dict:
    """体检回合：不检索、不生成，卡片和正文都由程序按 rag/healthcheck 的结果写。"""
    t = dict(turn, hits=[], queries=[], kind="health", health=compact_health(hc))
    return history_entry(t, health_brief(hc, label))


def drivers_entry(turn: dict, c: dict, label: str, web_sources: list[dict]) -> dict:
    """「几件大事」回合：正文和卡片由程序按 rag/drivers 的结果写；联网条目的 [网n] 对应 web_sources。"""
    t = dict(turn, hits=[], queries=[], kind="drivers", drivers=c, web_sources=list(web_sources or []))
    return history_entry(t, drivers_brief(c, label))


def plan_for(understanding: dict, *, current_company: str = "", current_year: str = "",
             current_period: str = FY, docs=(), today: date | None = None) -> dict:
    """规划器的理解（rag/planner.py）→ 用哪份报告。在 plan_resolution 外面多两条：

    · 反问、超范围：不找报告，返回 {"action": "clarify" | "out_of_scope"}；
    · 趋势：季报里只有本期和上年同期两个数，问多年必须用年报 —— 落在季报上就改找这家公司
      最新的年报（用户实际遇到的：停在三季报上问「近几年发展如何」，回答「年报中未找到」）。
    · 体检、几件大事：要用年报里才有的内容（体检 9 项里 6 项、风险章节和成本构成），
      落在季报上同样改用最新年报；用户明说了哪一期（「三季报体检」）就照他说的。
    """
    intent = understanding.get("intent")
    if intent in ("clarify", "out_of_scope"):
        return {"action": intent}
    kw = dict(current_company=current_company, current_year=current_year,
              current_period=current_period, docs=docs, today=today)
    tgt = understanding.get("target") or {}
    plan = plan_resolution(tgt, **kw)
    annual_first = intent == "trend" or (intent in ("health", "drivers") and not tgt.get("period"))
    if annual_first and plan["action"] in ("current", "switch"):
        landed = period_of(plan["doc"]) if plan["action"] == "switch" else current_period
        if landed != FY:
            # 年份要写明：留空的话 plan_resolution 会沿用当前季报的年份，而那一年的年报还没出
            annual = {"company": tgt.get("company") or "", "period": FY, "multiple": False,
                      "year": latest_year(FY, today)}
            plan = plan_resolution(annual, **kw)
    return plan


def _plan_latest(target: dict, company: str, here: bool, current_year: str,
                 current_period: str, docs) -> dict:
    """「最新财报」：本地这家公司报告期不早于 target 那一期的，取最新一份；没有就去巨潮。

    本地可能有比「已过截止日」更新的报告（提前披露、之前导入过），那份也算最新。
    """
    floor = (int(target["year"]), RANK[target["period"]])

    def key(d):
        return (int(d["year"]), RANK[period_of(d)])

    fresh = [d for d in docs
             if same_company(d.get("company", ""), company)
             and str(d.get("year", "")).isdigit() and key(d) >= floor
             and (target["latest"] != FY or period_of(d) == FY)]
    if fresh:
        best = max(fresh, key=lambda d: (key(d), bool(d.get("demo"))))
        if here and str(best["year"]) == str(current_year) and period_of(best) == current_period:
            return {"action": "current"}
        return {"action": "switch", "doc": best}
    plan = {"action": "import", "company": company, "year": target["year"],
            "period": target["period"]}
    if target.get("newer"):
        plan["newer"] = list(target["newer"])
    return plan


def adopt_turn(turn: dict | None, doc_id: str):
    """当前文档变了时，怎么处理进行中的回合。

    返回 `(保留的回合, (原文档 id, 要补写的历史条目) | None)`。

    切换年报必须丢弃进行中的回合，否则状态会串到另一份文档上。但**扩展分析 / 外部查询阶段**
    首轮回答已经显示出来了 —— 用户正看着它，切一下文档它就凭空消失，比之前只在
    生成阶段丢弃更糟（那时候用户还没看到任何东西）。所以先把它落进**原来那份**
    文档的历史，只是没有后续段，并记下对应状态为 `aborted`。
    """
    if not turn or turn.get("doc_id") == doc_id:
        return turn, None
    entry = None
    if turn.get("a"):
        mark = "web" if turn.get("status") == "searching" else "analyze"
        entry = history_entry(dict(turn, **{mark: "aborted"}), turn["a"])
    return None, ((turn.get("doc_id"), entry) if entry else None)


def run_parallel(jobs: dict) -> dict:
    """{名字: 无参函数} → {名字: (成功与否, 返回值或异常)}。复核和扩展分析都是网络调用，并行跑。"""
    out = {}
    with ThreadPoolExecutor(max(1, len(jobs))) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        for k, f in futs.items():
            try:
                out[k] = (True, f.result())
            except Exception as e:                   # 交给调用方按「这一路失败」处理
                out[k] = (False, e)
    return out


def _with_analysis(turn: dict, ok: bool, value) -> tuple[dict, str]:
    """扩展分析的结果并进回合：返回 (新回合, 扩展段正文)。失败不作废首轮回答。"""
    if ok:
        ext, diag = value
    else:
        ext, diag = "", {"analyze": "error", "analyze_error": str(value)}
    new = dict(turn, analyze=diag.get("analyze", ""))
    if diag.get("analyze_error"):
        new["analyze_error"] = diag["analyze_error"]
    return new, ext


def step(turn: dict, *, gen, ana, web=None, ind=None, client=None, company: str = "",
         year: str = "", recent: list[str] | None = None, rev=None, now=time.time):
    """推进「生成 → 扩展 / 外部查询 → 完成」这一段。

    参数：
      gen(question, hits, client=, company=, year=, history=) -> str
      ana(question, hits, first_answer, client=, company=, year=, history=) -> (str, dict)
      web(question, first_answer, client=, company=, year=, history=)
          -> (str, list[dict], dict)   # 补充段正文、网页来源、诊断
      ind(question, hits, client=, company=, year=, history=)
          -> (str, list[dict], dict)   # 行业动态：完整正文、网页来源、诊断

    返回 `(new_turn, entry, error)`：

      · `error` 非 None → 失败。调用方退款并把错误挂到回合上。
      · `entry` 非 None → 这一轮完成。调用方写进 history 并清掉进行中的回合。
      · 两者皆 None   → 状态变了（generating → analyzing / searching）。
                        调用方存回 session_state 并 rerun。

    只有传了 `web` 才会进入外部查询：首轮弃答时优先补充公开信息；
    没传 `web`（旧调用点、部分测试）时行为不变。

    传了 `rev(question, answer, hits, client=)` 就在首轮回答之后多一步 reviewing（rag/reviewer.py）：
    复核和扩展分析并行；不通过时按 decide() 的结论回退 —— 补检索（→ retrieving）、
    重新生成（→ generating）、重新理解（→ resolving），回退一次后不再回退。
    回退期间旧回答一直显示着，新回答出来再替换。

    **不修改传入的 turn**，返回的是新字典。跨 rerun 的会话状态最容易出的错，
    就是"就地改了但忘了写回 session_state" —— 用不可变风格把这类错挡在函数外面。
    """
    status = turn.get("status")

    # 行业动态是另一种回合：不走「年报回答 → 扩展」，一次联网检索直接成文。
    if status == "generating" and turn.get("kind") == "industry" and ind is not None:
        try:
            text, sources, diag = ind(turn["q"], turn["hits"], client=client,
                                      company=company, year=year, history=recent)
        except Exception as e:
            return turn, None, f"行业动态检索失败：{e}"
        new = dict(turn, a=text, web=diag.get("web", ""), web_sources=sources)
        return dict(new, status="done"), history_entry(new, text), None

    if status == "generating":
        try:
            answer = gen(turn["q"], turn["hits"], client=client,
                         company=company, year=year, history=recent)
        except Exception as e:
            return turn, None, f"调用大模型失败：{e}"

        # 模型偶尔一个字都不返回（端到端评测里见过）：重试一次，再空就报错退款，
        # 不能把空回答交给用户。
        if not (answer or "").strip():
            if not turn.get("empty_retry"):
                return dict(turn, empty_retry=True), None, None
            return turn, None, "模型没有返回内容，请重试。"
        new = dict(turn, a=answer)
        # 首轮明确说年报里没有：让模型调工具补公开信息，而不是凭记忆猜。
        # 弃答不做扩展（没有结论可解释，问"为什么"只会诱发编造）；
        # 纯查数的题也不做（实测 6/6 返回「无补充」，白调一次模型）。
        if web is not None and should_search(answer):
            nxt = "searching"
        elif should_analyze(answer, turn["q"]):
            nxt = "analyzing"
        else:
            nxt = "done"
        # 有复核时先复核：首轮说「未找到」也要先复核 —— 可能只是检索没找全，补检索比联网更该先试
        if rev is not None:
            return dict(new, status="reviewing", after_review=nxt), None, None
        if nxt != "done":
            return dict(new, status=nxt), None, None
        return dict(new, status="done"), history_entry(new, answer), None

    if status == "reviewing":
        nxt = turn.get("after_review", "done")
        jobs = {"review": lambda: rev(turn["q"], turn["a"], turn["hits"], client=client)}
        if nxt == "analyzing":
            jobs["analyze"] = lambda: ana(turn["q"], turn["hits"], turn["a"], client=client,
                                          company=company, year=year, history=recent)
        results = run_parallel(jobs)
        ok, res = results["review"]
        res = res if ok else None
        rounds = turn.get("round", 0)
        action = decide(res, round_no=rounds, elapsed=now() - turn.get("started", now()))
        base = {k: v for k, v in turn.items() if k != "after_review"}
        if action in ("retrieve", "regenerate", "replan"):
            # 回退：旧回答留着显示（界面挂「复核发现…」），新的一轮出来再替换
            new = dict(base, round=rounds + 1, retry=action, review_last=res)
            if action == "retrieve":
                return dict(new, status="retrieving", retry_queries=res["queries"]), None, None
            if action == "regenerate":
                return dict(new, status="generating"), None, None
            # 重新理解可能换报告：旧报告的指标卡不能留到新报告上
            new = {k: v for k, v in new.items() if k not in CARD_KEYS}
            return dict(new, status="resolving", replan_hint=res.get("note", "")), None, None
        verdict = "error" if res is None else ("pass" if action == "pass" else "fail")
        new = dict(base, review=summary(res, rounds, verdict))
        if nxt == "analyzing":
            new, ext = _with_analysis(new, *results["analyze"])
            return dict(new, status="done"), history_entry(new, join_answer(turn["a"], ext)), None
        if nxt == "searching":
            return dict(new, status="searching"), None, None
        return dict(new, status="done"), history_entry(new, turn["a"]), None

    if status == "searching":
        diag: dict = {}
        ext = ""
        sources: list[dict] = []
        if web is None:
            return (dict(turn, status="done", web="skipped"),
                    history_entry(dict(turn, web="skipped"), turn["a"]), None)
        try:
            ext, sources, diag = web(turn["q"], turn["a"], client=client,
                                     company=company, year=year, history=recent)
        except Exception as e:
            # 和扩展分析同理：首轮回答是完整的，补充失败不能让它作废。
            diag = {"web": "error", "web_error": str(e)}
        new = dict(turn, web=diag.get("web", ""), web_sources=sources)
        if diag.get("web_error"):
            new["web_error"] = diag["web_error"]
        return (dict(new, status="done"),
                history_entry(new, join_external(turn["a"], ext)), None)

    if status == "analyzing":
        # 首轮回答本身是完整的，不能因为更晚的一次**补充**调用而作废它。
        # 失败记在回合上（测量与排查用），用户拿到的仍是一份可用的回答。
        try:
            res = (True, ana(turn["q"], turn["hits"], turn["a"], client=client,
                             company=company, year=year, history=recent))
        except Exception as e:
            res = (False, e)
        new, ext = _with_analysis(turn, *res)
        return (dict(new, status="done"),
                history_entry(new, join_answer(turn["a"], ext)), None)

    return turn, None, f"未知的回合状态：{status}"
