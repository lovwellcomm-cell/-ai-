"""结构化财务数据 → 组件的指标卡（kpis / charts / cardNote）。纯函数，不碰 Streamlit。

比率在这里用 Python 算：归母净利率、净现比。模型不参与，所以不会有「自算不一致」。
增减优先用公司披露的比例（rag/financials.py 已经用它做过复算校验）。
"""
from __future__ import annotations

import re

from rag.periods import FY, MONTHS, Q3, SHORT, SPAN

# 指标卡随消息携带的字段。回合写进历史（ui/turn.py）和下发给组件（ui/payload.py）
# 都按这份清单 —— 漏掉一处，卡片就会在回合结束时消失。health 是体检卡（rag/healthcheck.compact），
# drivers 是「影响这家公司的几件大事」（rag/drivers.card）。
CARD_KEYS = ("kpis", "charts", "cardTitle", "cardNote", "health", "drivers")

# 只有问到这些方向时才出卡。问前五大客户、研发资本化这类问题，一排利润指标就是答非所问。
_WANTS_RE = re.compile(
    r"营收|收入|利润|净利|盈利|赚|业绩|财务|财报|经营(情况|状况|成果)|现金流|资产|负债|"
    r"增长|下滑|下降|趋势|同比|毛利|利润率|ROE|净资产收益率|摘要|概况|基本面|怎么样",
    re.IGNORECASE,
)
# 明确在问别的维度时，即使出现「如何」「变化」也不出卡。
_OTHER_RE = re.compile(r"客户|供应商|研发|员工|人员|股东|分红|董事|高管|诉讼|担保|募集|专利|产能|门店")


# 「那2024年呢？」「去年呢」这类短追问本身不带话题，话题跟着上一问走。
_FOLLOWUP_RE = re.compile(r"^(那|那么)?.{0,8}(呢|如何|怎样|怎么样)[？?]?$")


def wants_card(question: str, previous: list[str] | tuple[str, ...] = ()) -> bool:
    """这一问要不要出指标卡。previous 是同一段对话里此前的问题（旧 → 新）。"""
    q = (question or "").strip()
    # 只换了年份/口径的短追问：话题跟着前文走（前面问客户，「那2023年怎么样」也是问客户）。
    # 连续追问（「那2024年呢」→「那2023年呢」）要一直往回找到最近一个有话题的问题。
    # 换到另一年的年报时，卡片就是那一年的。
    if previous and _FOLLOWUP_RE.match(q):
        *earlier, last = previous
        return wants_card(last, earlier)
    if _OTHER_RE.search(q) and not re.search(r"营收|收入|利润|现金流|财务|业绩", q):
        return False
    return bool(_WANTS_RE.search(q))


def _scale_for(values: list[float]) -> tuple[float, str]:
    """统一用亿元；整组最大绝对值不到 1 亿时改用万元，免得全是 0.0x。"""
    peak = max((abs(v) for v in values), default=0)
    return (1e8, "亿元") if peak >= 1e8 else (1e4, "万元")


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def _series(fin: dict, key: str, years: list[str] | None = None) -> list[float] | None:
    years = years or fin["years"]
    item = fin["items"].get(key)
    if not item or any(y not in item["values"] for y in years):
        return None
    return [item["values"][y] for y in reversed(years)]   # 旧 → 新


def _amount_kpi(fin: dict, key: str, label: str) -> dict | None:
    item = fin["items"].get(key)
    if not item:
        return None
    cur = item["values"][fin["years"][0]]
    div, unit = _scale_for([cur])
    kpi = {"k": label, "v": _fmt(cur / div), "u": unit}
    change = item.get("change")
    if change is not None:
        kpi["d"] = f"{abs(change):.2f}%"
        kpi["up"] = change >= 0
    return kpi


def _quarter_kpi(fin: dict, key: str, label: str) -> dict | None:
    """三季报的单季数（7–9 月），增减用公司披露的单季同比。"""
    q = (fin["items"].get(key) or {}).get("quarter")
    if not q:
        return None
    div, unit = _scale_for([q["value"]])
    kpi = {"k": label, "v": _fmt(q["value"] / div), "u": unit,
           "d": f"{abs(q['change']):.2f}%", "up": q["change"] >= 0}
    return kpi


def _ratio(fin: dict, num: str, den: str, year: str) -> float | None:
    a, b = fin["items"].get(num), fin["items"].get(den)
    if not a or not b:
        return None
    d = b["values"].get(year)
    n = a["values"].get(year)
    if n is None or not d:
        return None
    return n / d


def _margin_kpi(fin: dict) -> dict | None:
    """归母净利率 = 归母净利润 ÷ 营业收入；变化用百分点。"""
    y0, y1 = fin["years"][0], fin["years"][1]
    cur = _ratio(fin, "np_parent", "revenue", y0)
    if cur is None:
        return None
    kpi = {"k": "归母净利率", "v": f"{cur * 100:.2f}", "u": "%"}
    prev = _ratio(fin, "np_parent", "revenue", y1)
    if prev is not None:
        diff = (cur - prev) * 100
        kpi["d"] = f"{abs(diff):.2f}pct"
        kpi["up"] = diff >= 0
    return kpi


def _cash_kpi(fin: dict) -> dict | None:
    """净现比 = 经营现金流净额 ÷ 归母净利润，衡量利润有多少收成了现金。
    净利润 ≤ 0 时这个比值没有意义（负负得正会显得很好看），不出。"""
    y0, y1 = fin["years"][0], fin["years"][1]
    np_item = fin["items"].get("np_parent")
    if not np_item or np_item["values"].get(y0, 0) <= 0:
        return None
    cur = _ratio(fin, "ocf", "np_parent", y0)
    if cur is None:
        return None
    kpi = {"k": "净现比", "v": f"{cur:.2f}", "u": "倍"}
    # 上年比值 ≤ 0（上年经营现金流为负，比如精达 2024）时，和它比的「变化」没有意义 ——
    # 会显示成「▲ 5.18」这种看不懂的数，干脆不出。
    if np_item["values"].get(y1, 0) > 0:
        prev = _ratio(fin, "ocf", "np_parent", y1)
        if prev is not None and prev > 0:
            kpi["d"] = f"{abs(cur - prev):.2f}倍"
            kpi["up"] = cur >= prev
    return kpi


def _cagr(vals: list[float]) -> str | None:
    """复合增速。首尾任一年 ≤ 0 时没有意义（亏损转盈利算不出「年均增长」），不出。"""
    first, last, n = vals[0], vals[-1], len(vals) - 1
    if n < 2 or first <= 0 or last <= 0:
        return None
    return f"{((last / first) ** (1 / n) - 1) * 100:+.2f}%"


def _chart(fin: dict, key: str, title: str) -> dict | None:
    """三年画柱；接上往年年报（rag/history.py）后画折线，另给复合增速。
    某个科目在往年那份里没抽到时，这张图退回三年，不画断开的折线。"""
    years = fin["years"]
    vals = _series(fin, key, years)
    if not vals and len(years) > 3:
        years = years[:3]
        vals = _series(fin, key, years)
    if not vals:
        return None
    div, unit = _scale_for(vals)
    labels = list(reversed(years))
    yoy = ["—"]
    for prev, cur in zip(vals, vals[1:]):
        yoy.append("—" if not prev else f"{(cur - prev) / abs(prev) * 100:+.2f}%")
    chart = {
        "title": title,
        "range": f"{labels[0]}–{labels[-1]}",
        "unit": unit,
        "labels": labels,
        "values": [round(v / div, 2) for v in vals],
        "yoy": yoy,
    }
    if len(vals) > 3:
        chart["kind"] = "line"
        cagr = _cagr(vals)
        if cagr:
            chart["cagr"] = f"{len(vals) - 1} 年复合增速 {cagr}"
    return chart


def _quarter_chart(q: dict, key: str, title: str) -> dict | None:
    """单季折线（rag/quarters.series）：悬停里的「同比」是和四个季度前比，不是相邻两季。"""
    item = (q.get("items") or {}).get(key)
    if not item:
        return None
    vals, labels = item["values"], item["labels"]
    div, unit = _scale_for(vals)
    yoy = []
    for i, cur in enumerate(vals):
        prev = vals[i - 4] if i >= 4 else None
        yoy.append("—" if not prev else f"{(cur - prev) / abs(prev) * 100:+.2f}%")
    return {"title": title, "range": f"{labels[0]}–{labels[-1]}", "unit": unit, "kind": "line",
            "labels": list(labels), "values": [round(v / div, 2) for v in vals], "yoy": yoy}


def _note(fin: dict) -> str:
    period = fin.get("period") or FY
    if period == FY and fin.get("sources"):
        parts = [f"{s['years']} 出自 {s['label']}第 {s['page']} 页" for s in fin["sources"]]
        return " · ".join(["程序抽取", *parts, "均已与披露增减核对"])
    if period == FY:
        return f"程序抽取 · 年报第 {fin['page']} 页 · 已与披露增减核对"
    check = "已与利润表、现金流量表核对" if period == Q3 else "已与披露增减核对"
    audited = fin.get("audited")
    audit = "未经审计" if audited is False else ("" if audited else "通常未经审计")
    return " · ".join(x for x in (f"程序抽取 · {SHORT[period]}第 {fin['page']} 页", check, audit) if x)


def build_card(fin: dict | None, quarters: dict | None = None) -> dict | None:
    """返回要合并到回合上的 {kpis, charts, cardTitle, cardNote}；数据不够时返回 None。

    年报：近三年。季报 / 半年报：本期与上年同期两根柱子；三季报的金额是「年初至今」（1–9 月），
    另加两张单季（7–9 月）指标卡 —— 投资者看三季报最关心的往往是单季的拐点。
    quarters（rag/quarters.series，问趋势时才有）：两张图换成最近 8 季的单季折线，指标卡不变。
    """
    if not fin or not fin.get("items"):
        return None
    period = fin.get("period") or FY
    head = [
        _amount_kpi(fin, "revenue", "营业收入"),
        _amount_kpi(fin, "np_parent", "归母净利润"),
        _amount_kpi(fin, "np_deducted", "扣非净利润"),
        _amount_kpi(fin, "ocf", "经营现金流净额"),
    ]
    if period == Q3:
        tail = [_quarter_kpi(fin, "revenue", "Q3 单季营收"),
                _quarter_kpi(fin, "np_parent", "Q3 单季归母净利")]
    else:
        tail = [_margin_kpi(fin), _cash_kpi(fin)]
    kpis = [k for k in head + tail if k]
    if len(kpis) < 2:
        return None
    span = "" if period == FY else f"（{MONTHS[period]}）"
    charts = [c for c in (_chart(fin, "revenue", "营业收入" + span),
                          _chart(fin, "np_parent", "归母净利润" + span)) if c]
    qcharts = [c for c in (_quarter_chart(quarters or {}, "revenue", "单季营业收入"),
                           _quarter_chart(quarters or {}, "np_parent", "单季归母净利润")) if c]
    title = f"主要会计数据 · {fin['years'][0]}"
    if period != FY:
        title += f" {SPAN[period]}"
    note = _note(fin)
    if qcharts:
        charts = qcharts
        title += f" · 近 {max(len(c['values']) for c in qcharts)} 季单季"
        note += (f" · 单季据{'、'.join(quarters['sources'])}的累计数相减"
                 "（第三季度用三季报披露的单季数）")
    elif period == FY and any(c.get("kind") == "line" for c in charts):
        title += f" · 近 {len(fin['years'])} 年"
    return {"kpis": kpis, "charts": charts, "cardTitle": title, "cardNote": note}
