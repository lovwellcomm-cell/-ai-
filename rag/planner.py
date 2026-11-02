"""agent1 规划器：一次模型调用把用户的问题「理解」成一份结构化计划。

以前「理解问题」散在三处：rag/company.py 用一次模型调用认公司和年份，rag/retriever.py 再用一次
改写检索词，趋势 / 行业 / 出卡靠关键词规则。三处互不知道对方，于是出现过：
  · 停在三季报上问「近几年发展如何」—— 检索词改写得不错，但没人意识到三季报里只有两年的数，
    该换年报（tools/eval_intent.py 的 trend-6，用户实际遇到的）；
  · 「今天天气怎么样」触发指标卡（规则只看到了「怎么样」）；
  · 「对比一下」「那个数据是多少」没有上文也硬猜着答。

这里合并成一次调用，输出意图、目标报告、要回答的要点、检索词、反问选项。模型不可全信，所以：
  · 公司名必须在问题或前文里真出现过，年份必须是问题或前文里写过的 —— 防臆造；
  · 报告期、「最新」、相对年份、行业动态这些**有测试的规则一旦命中就以规则为准**；
  · 调用失败或输出不合规 → 调用方退回旧流程（rag/company.py + 规则），不会比原来差。

纯函数为主：模型调用只在 understand() 里，其余都能直接测。
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import config
from .company import _appears, year_in
from .history import wants_trend
from .industry import wants_industry
from .periods import PERIODS, SHORT, report_label
from ui.card import _OTHER_RE

# 明确的财务科目 / 比率；客户、研发这类别的话题不算（和 ui/card.py 的 _OTHER_RE 同口径）
_FIN_RE = re.compile(r"营收|收入|利润|净利|盈利|毛利|ROE|净资产收益率|现金流|负债率|资产负债|业绩|财报|财务",
                     re.IGNORECASE)

INTENTS = ("report", "trend", "industry", "clarify", "out_of_scope")

PROMPT = """你是上市公司定期报告（年报、季报）问答助手的「问题理解」模块。不要回答问题，只把它理解清楚，输出 JSON。

【输出字段】
- restated：把指代和省略补全后的完整问题（结合对话历史和当前报告）。保留用户原意，不添加用户没问的内容。
- intent：五选一
  · trend：问多年的变化、发展、成长性、走势、长期表现（"近几年发展如何""这几年增长快吗""长期盈利能力"）—— 需要多个年度的数据。
  · industry：问行业动态、竞争对手近况、原材料价格行情、政策变化等报告之外的近期公开信息。
  · out_of_scope：和上市公司定期报告无关（闲聊、写作、天气），或要求荐股、预测股价、查实时股价。
    问**任何一家上市公司**（包括不是当前报告的公司、本地没有的公司）的经营和财务，都不是 out_of_scope ——
    系统会自动换到那家公司的报告；问多家公司对比也不是。
  · clarify：问题缺少关键对象，结合对话历史和当前报告也补不出来（没有上文时的"对比一下""那个数据是多少"）。能合理理解时不要反问。
  · report：其他所有能用定期报告回答的问题。
- company：问题或对话历史里**明确出现**的公司名，按原文写；"它""该公司"这类代词不算；没有就填空字符串。
- multiple：问题里出现两家及以上公司时为 true，company 填第一家。
- year：只填问题里写出的 4 位年份；追问省略年份时（前文问 2025 年，现在"那 2024 年呢"）填 2024；"去年""最新""近几年"不要换算，填 null。
- period：问题明确提到季度、半年、年报时填 "Q1" / "H1" / "Q3" / "FY"，否则 null。
- card：要不要附营收、利润、现金流等主要财务指标卡。问营收、利润、毛利率、净利率、ROE、现金流、资产负债这类财务数字或比率，或问整体业绩、财务状况、增长、发展时 true；
  只问客户、供应商、研发、分红、人员、某个非财务话题时 false；trend 一定 true；industry、out_of_scope、clarify 一定 false。
- subquestions：把问题拆成 1–4 个需要分别回答的要点（简短名词短语）。
- queries：2–4 条在报告里检索用的短语，用报告原文的规范用语：去掉公司名；口语换成会计科目或栏目名
  （"赚了多少钱"→"归属于上市公司股东的净利润"）；涉及具体财务数字时加一条带表名的（"主要会计数据 营业收入"）；
  问变化时加"本期比上年同期增减"；trend 要包含"主要会计数据"。
- clarify：intent 为 clarify 时填 {{"question": "反问的一句话", "options": ["选项1", "选项2", "选项3"]}}，
  选项 2–4 个，每个都是**可以直接发送的完整问题**，结合当前报告来写，只针对一家公司（系统不支持多家公司对比）；
  否则填 {{"question": "", "options": []}}。
- reply：intent 为 out_of_scope 时，用一句话礼貌说明这里只能回答定期报告里的内容，可以举一个能问的例子；否则填空字符串。

只输出 JSON：
{{"restated": "", "intent": "report", "company": "", "multiple": false, "year": null, "period": null,
"card": false, "subquestions": [], "queries": [], "clarify": {{"question": "", "options": []}}, "reply": ""}}"""


def _context(question: str, history: list, current: dict, docs: list[dict], today: date,
             hint: str = "") -> str:
    """喂给模型的用户消息：今天、当前报告、本地这家公司有哪些报告、对话历史、问题。"""
    company = current.get("company", "")
    cur = f"{company} {report_label(current.get('year', ''), current.get('period') or 'FY')}".strip()
    mine = sorted({report_label(d.get("year", ""), d.get("period") or "FY")
                   for d in docs if d.get("company") == company and d.get("year")}, reverse=True)
    rows = [f"【今天】{today.isoformat()}", f"【当前报告】{cur or '无'}"]
    if mine:
        rows.append(f"【本地已有{company}的报告】{'、'.join(mine)}")
    hist = []
    for h in history or []:
        if isinstance(h, dict):
            if h.get("q"):
                hist.append(f"用户：{h['q']}")
            if h.get("gist"):
                hist.append(f"助手（结论摘要）：{h['gist']}")
        elif h:
            hist.append(f"用户：{h}")
    if hist:
        rows.append("【对话历史】\n" + "\n".join(hist))
    if hint:
        # 复核判定上一次理解错了（rag/reviewer.py 的 replan）：把它的意见交给这一次
        rows.append(f"【上一次的理解被复核否定】{hint}")
    rows.append(f"【问题】{question}")
    return "\n".join(rows)


def _str_list(v, n: int, limit: int) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [str(x).strip()[:limit] for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return list(dict.fromkeys(out))[:n]


def _history_text(history: list) -> str:
    return " ".join((h.get("q", "") if isinstance(h, dict) else str(h or "")) for h in history or [])


def normalize(data: dict, question: str, history: list, today: date) -> dict | None:
    """模型输出 → 校验过的计划。输出不成形（不是 dict、没有 intent）时返回 None，调用方退回旧流程。"""
    from ui.turn import with_period                      # 报告期 / 最新 / 相对年份的规则

    if not isinstance(data, dict) or data.get("intent") not in INTENTS:
        return None
    intent = data["intent"]
    past = _history_text(history)

    company = str(data.get("company") or "").strip()[:30]
    if company and not (_appears(company, question) or _appears(company, past)):
        company = ""                                     # 问题和前文里都没有 → 模型臆造的
    year = data.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    if year is not None and (not 1990 <= year <= 2100 or str(year) not in question + past):
        year = None
    if year is None:
        year = year_in(question)
    period = data.get("period") if data.get("period") in PERIODS else None

    # 规则优先：问题里说清了报告期、「最新」、「去年三季度」时用规则的结果
    target = with_period({"company": company, "year": year,
                          "multiple": bool(data.get("multiple"))}, question, today)
    if target.get("period") is None and not target.get("latest") and period:
        target["period"] = period

    # 意图：行业动态、趋势的关键词规则有测试，命中就算数；反问和超范围只认模型
    if wants_industry(question) and intent in ("report", "trend"):
        intent = "industry"
    elif intent == "report" and wants_trend(question, [h.get("q", h) if isinstance(h, dict) else h
                                                       for h in history or []]):
        intent = "trend"

    # 问的是某家公司（或多家对比）就一定在范围内：模型曾把「茅台2024年营收多少」判成超范围
    if intent == "out_of_scope" and (target.get("company") or target.get("multiple")):
        intent = "report"

    clarify = data.get("clarify") if isinstance(data.get("clarify"), dict) else {}
    options = _str_list(clarify.get("options"), 4, 60)
    ask = str(clarify.get("question") or "").strip()[:120]
    if intent == "clarify" and len(options) < 2:
        intent = "report"                                # 没给出像样的选项就别反问，照常回答
    # 问到明确的财务科目或比率时一定出卡（模型漏判过「毛利率」「ROE」）。
    # 不用 ui/card.wants_card 整条规则：它会被「怎么样」单独触发（「今天天气怎么样」也出卡）。
    card = bool(data.get("card")) or (bool(_FIN_RE.search(question)) and not _OTHER_RE.search(question))
    if intent == "trend":
        card = True
    elif intent in ("industry", "out_of_scope", "clarify"):
        card = False
    return {
        "source": "planner",
        "intent": intent,
        "target": target,
        "restated": str(data.get("restated") or "").strip()[:200],
        "subquestions": _str_list(data.get("subquestions"), 4, 80),
        "queries": _str_list(data.get("queries"), 4, 60),
        "card": card,
        "clarify": {"question": ask or "想确认一下你想问的是：", "options": options}
        if intent == "clarify" else None,
        "reply": str(data.get("reply") or "").strip()[:200] if intent == "out_of_scope" else "",
    }


def understand(question: str, *, client, history: list | None = None,
               current: dict | None = None, docs: list[dict] | None = None,
               today: date | None = None, hint: str = "") -> dict | None:
    """调一次模型拿计划。任何失败（超时、截断、不合规）都返回 None —— 调用方退回旧流程。"""
    today = today or date.today()
    current = current or {}
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": _context(question, history or [], current,
                                                           docs or [], today, hint)}],
            temperature=0, max_tokens=config.PLANNER_MAX_TOKENS, timeout=config.PLANNER_TIMEOUT,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "")
    except Exception:
        return None
    return normalize(data, question, history or [], today)


def fallback(question: str, *, client=None, history: list | None = None,
             current: dict | None = None, today: date | None = None) -> dict:
    """旧流程，形状和 understand() 一样：模型认公司年份 + 规则补报告期和意图。
    queries 为空 —— 检索时照旧由 rag/retriever.py 自己改写。"""
    from rag.company import extract_target
    from ui.card import wants_card
    from ui.turn import with_period

    current = current or {}
    previous = [h.get("q", "") if isinstance(h, dict) else h for h in history or []]
    try:
        tgt = extract_target(question, client=client, current_company=current.get("company", ""))
    except Exception:
        tgt = {"company": "", "year": None, "multiple": False}
    tgt = with_period(tgt, question, today)
    card = wants_card(question, previous=previous)
    if wants_industry(question):
        intent = "industry"
    elif card and wants_trend(question, previous):
        intent = "trend"
    else:
        intent = "report"
    return {"source": "fallback", "intent": intent, "target": tgt, "restated": "",
            "subquestions": [], "queries": [], "card": card, "clarify": None, "reply": ""}


def focus_block(plan: dict | None) -> str:
    """交给生成环节的「问题理解」：补全后的问题 + 要回答的要点。原问题仍是最终依据。"""
    if not plan or plan.get("source") != "planner":
        return ""
    rows = []
    if plan.get("restated"):
        rows.append(f"理解为：{plan['restated']}")
    if plan.get("subquestions"):
        rows.append("需要逐一回答：" + "；".join(plan["subquestions"]))
    if not rows:
        return ""
    return ("【问题理解】仅帮助你把握问题，和【问题】冲突时以【问题】为准；"
            "片段里没有的要点照规则说明未找到，不要编造。\n" + "\n".join(rows) + "\n\n")


def describe(plan: dict | None) -> str:
    """检索轨迹里给人看的一行。"""
    if not plan:
        return ""
    names = {"report": "答报告", "trend": "拉长趋势", "industry": "行业动态",
             "clarify": "反问澄清", "out_of_scope": "超出范围"}
    t = plan.get("target") or {}
    who = " ".join(x for x in (t.get("company") or "", str(t.get("year") or ""),
                               SHORT.get(t.get("period") or "", "")) if x)
    parts = [names.get(plan.get("intent"), "")]
    if who:
        parts.append(who)
    if plan.get("restated"):
        parts.append(plan["restated"])
    return " · ".join(p for p in parts if p)
