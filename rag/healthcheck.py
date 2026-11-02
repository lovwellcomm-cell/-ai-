"""年报小字体检：把散户不会翻、但很关键的披露项抽出来，按公开的规则亮红黄绿灯。

和 financials.py 一样是确定性抽取：每一项都有原文页码，抽不到就标「未找到」，不猜；
红黄绿的阈值写在 RULES 里，界面原样展示给用户。模型不参与判断。

真实年报里踩到的格式差异（夹具就是这些原文）：
  · 勾选框：上交所保留「√」（`□适用 √不适用`）；深交所的 √ 提取后丢失（`□适用 不适用` = 勾了不适用），
    所以按「哪个选项前没有 □」判断
  · 前五名客户：上交所是句子（`前五名客户销售额…万元，占年度销售总额57.09%`），深交所是表
    （`前五名客户合计销售金额占年度销售总额比例 65.04%`）；万科没有这一节
  · 审计意见：上交所在第 2 页「出具了标准无保留意见的审计报告」，深交所在审计报告前一页
    「审计意见类型 标准的无保留意见」；正文里「我们应当发表非无保留意见」是每份都有的套话，不能当依据
  · 金额会粘连：`23,902,060.2254,462,291.37` 是两个数；万科的负数写成 `(6,038,699,619.35)`
  · 正文里的「质押」多是公司拿存单质押，大股东质押只看「累计质押…80%以上」那一项勾选
"""
from __future__ import annotations

import json
import re

from . import config
from .financials import UNIT_SCALE
from .periods import FY

HC_VERSION = 1
HC_FILE = "healthcheck.json"

GREEN, YELLOW, RED, NONE = "green", "yellow", "red", "none"
_RANK = {NONE: -1, GREEN: 0, YELLOW: 1, RED: 2}

# 卡片上逐项公开的判断标准。改阈值时同步改这里的文字 —— 用户看到的就是这份。
RULES = {
    "audit": "绿：标准无保留意见；黄：带强调事项段；红：保留、否定、无法表示意见，或提示持续经营重大不确定性",
    "accounting": "绿：未重述以前年度数据；黄：重述过以前年度数据，或有会计变更/差错更正需看原文；红：更正了重大会计差错",
    "cash": "看经营现金流 ÷ 归母净利润。绿：≥ 0.8；黄：0–0.8，或亏损但现金流为正；红：< 0，或亏损且现金流为负",
    "profit": "看非经常性损益占归母净利润。绿：< 20%；黄：20%–50%，或政府补助占净利润 ≥ 30%；红：≥ 50%，或亏损",
    "customers": "前五名客户占销售额。绿：< 50%；黄：50%–80%；红：≥ 80%",
    "suppliers": "前五名供应商占采购额。绿：< 50%；黄：50%–80%；红：≥ 80%",
    "related": "前五名客户/供应商中关联方的占比（取较大者）。绿：< 10%；黄：10%–30%；红：≥ 30%",
    "goodwill": "商誉 ÷ 归母净资产。绿：< 10%；黄：10%–30%；红：≥ 30%",
    "pledge": "控股股东累计质押超过所持股份 80% 时为红，否则为绿",
}
LABELS = {
    "audit": "审计意见", "accounting": "会计调整", "cash": "现金流含金量",
    "profit": "利润含金量", "customers": "客户集中度", "suppliers": "供应商集中度",
    "related": "关联交易（前五名中）", "goodwill": "商誉", "pledge": "大股东质押",
}
ORDER = tuple(RULES)
ANNUAL_ONLY = ("audit", "accounting", "customers", "suppliers", "related", "pledge")

# 两位小数的金额。左右不能挨着数字，免得把 7757.81 读成 757.81；百分比不算金额。
_AMT_RE = re.compile(r"(?<![\d.,])[(（]?[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}[)）]?(?![\d%])")
# 粘连的两个金额：两位小数后面紧跟数字，就在中间断开
_GLUE_RE = re.compile(r"(\.\d{2})(?=\d)")
_UNIT_RE = re.compile(r"单位\s*[:：]\s*(亿元|万元|千元|元)|^\s*人民币(元)\s")
_CJK = r"一-鿿"


# ---------------------------------------------------------------- 小工具

def _amounts(text: str) -> list[float]:
    """一段文字里的金额（按出现顺序）。括号或负号表示负数。"""
    out = []
    for m in _AMT_RE.finditer(_GLUE_RE.sub(r"\1 ", text)):
        raw = m.group(0)
        v = float(re.sub(r"[^\d.]", "", raw))
        out.append(-v if raw[0] in "-−(（" else v)
    return out


def _checked(text: str, options: tuple[str, str]) -> str | None:
    """勾选框 → 被勾的那个选项。options 里长的写前面（「不适用」包含「适用」）。

    有 √ 就认 √；没有 √（深交所提取后丢了）就认前面没有 □ 的那个。两个都看不出来返回 None。
    """
    t = re.sub(r"\s+", "", text)
    found = re.findall("([□√☑■✓]?)(" + "|".join(options) + ")", t)[:2]
    if len(found) < 2 or {o for _, o in found} != set(options):
        return None
    for box, opt in found:
        if box and box != "□":
            return opt
    unboxed = [o for box, o in found if not box]
    return unboxed[0] if len(unboxed) == 1 else None


def _flat(pages_lines: list[list[str]]) -> list[str]:
    """每页一段去掉空白的文字，用来找跨行的句子。"""
    return [re.sub(r"\s+", "", "".join(lines)) for lines in pages_lines]


def _find(flat: list[str], anchor: str, after: int = 40,
          pages: range | None = None) -> tuple[int, str] | None:
    """第一处 anchor → (页码, anchor 后面 after 个字)。页尾不够就接下一页开头。"""
    for i in pages or range(len(flat)):
        if i >= len(flat):
            break
        pos = flat[i].find(anchor)
        if pos < 0:
            continue
        tail = flat[i][pos + len(anchor):]
        if len(tail) < after and i + 1 < len(flat):
            tail += flat[i + 1]
        return i + 1, tail[:after]
    return None


def _unit_before(lines: list[str], upto: int) -> str | None:
    """本页 upto 行之前最后一处「单位：万元」。"""
    unit = None
    for ln in lines[:upto]:
        m = _UNIT_RE.search(ln)
        if m:
            unit = m.group(1) or m.group(2)
    return unit


def _yi(v: float) -> str:
    """金额（元）→「12.34 亿元」「5,678.90 万元」。"""
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f} 亿元"
    if abs(v) >= 1e4:
        return f"{v / 1e4:,.2f} 万元"
    return f"{v:,.2f} 元"


def _pct(v: float) -> str:
    return f"{v:.2f}%"


def _grade(v: float, yellow: float, red: float) -> str:
    return RED if v >= red else YELLOW if v >= yellow else GREEN


def _item(key: str, status: str, value: str = "", summary: str = "",
          page: int | None = None, detail: str = "") -> dict:
    return {"key": key, "label": LABELS[key], "status": status, "value": value,
            "summary": summary, "detail": detail, "page": page, "rule": RULES[key]}


def _missing(key: str, why: str = "未找到") -> dict:
    return _item(key, NONE, why, "报告里没有找到这一项的披露，不作判断。" if why == "未找到" else why)


def _fin_value(fin: dict | None, key: str, year: str) -> float | None:
    try:
        return float(fin["items"][key]["values"][year])
    except (TypeError, KeyError, ValueError):
        return None


# ---------------------------------------------------------------- 审计意见

_AUDIT_SENT_RE = re.compile(r"出具了(.{2,40}?)的?审计报告")
_AUDIT_TYPE_RE = re.compile(r"^审计意见类型\s*(\S.{1,40})$")
_AUDIT_HEAD_RE = re.compile(
    r"^[一二三四五六七八九十]、\s*(?:形成)?(强调事项|与持续经营相关的重大不确定性|"
    r"保留意见的基础|否定意见的基础|无法表示意见的基础)")


def _audit_opinion(pages_lines, flat) -> tuple[str, int] | None:
    for i in range(min(5, len(flat))):
        for m in _AUDIT_SENT_RE.finditer(flat[i]):
            if "内部控制" not in m.group(1):
                return m.group(1), i + 1
    for i, lines in enumerate(pages_lines):
        for ln in lines:
            m = _AUDIT_TYPE_RE.match(ln.strip())
            if m:
                return re.sub(r"\s+", "", m.group(1)), i + 1
    return None


def check_audit(pages_lines, flat) -> dict:
    found = _audit_opinion(pages_lines, flat)
    heads = {}
    for i, lines in enumerate(pages_lines):
        for ln in lines:
            m = _AUDIT_HEAD_RE.match(ln.strip())
            if m:
                heads.setdefault(m.group(1), i + 1)
    if not found and not heads:
        return _missing("audit")
    opinion, page = found or ("", None)
    o = opinion
    going = "持续经营" in o or "与持续经营相关的重大不确定性" in heads
    if "否定" in o or "无法表示" in o or "否定意见的基础" in heads or "无法表示意见的基础" in heads:
        status, summary = RED, "会计师对财务报表给了否定或无法表示意见，报表的可信度存疑。"
    elif ("保留意见" in o and "无保留" not in o) or "保留意见的基础" in heads:
        status, summary = RED, "会计师给了保留意见：报表有一部分会计师不认可。"
    elif going:
        status = RED
        summary = "会计师提醒：公司能否持续经营存在重大不确定性。这是审计报告里最严重的提示之一。"
        page = heads.get("与持续经营相关的重大不确定性", page)
    elif "强调事项" in o or "强调事项" in heads:
        status, summary = YELLOW, "审计意见带「强调事项段」：会计师特别提醒读者关注某件事。"
        page = page or heads.get("强调事项")
    elif "无保留" in o and "标准" in o:
        status, summary = GREEN, "会计师出具了标准无保留意见，没有特别保留或提示。"
    else:
        status, summary = YELLOW, "审计意见不是标准表述，建议翻到原页看会计师具体怎么写。"
    return _item("audit", status, opinion or "见原页", summary, page)


# ---------------------------------------------------------------- 会计调整

_RESTATE_RE = re.compile(r"是否(?:因会计政策变更及会计差错更正等)?需?追溯调整或重述以前年度会计数据")


def check_accounting(flat) -> dict:
    restated = page = reason = None
    for i, text in enumerate(flat):
        m = _RESTATE_RE.search(text)
        if m:
            tail = text[m.end():m.end() + 20]
            restated = _checked(tail, ("是", "否"))
            page = i + 1
            if restated == "是":
                r = _find(flat, "追溯调整或重述原因", 12, range(i, i + 2))
                # 原因后面紧跟表头「2025年」，截到第一个数字为止
                reason = re.match(r"[^\d]{0,10}", r[1]).group(0) or None if r else None
            break
    error = _find(flat, "重大会计差错更正原因及影响的分析说明", 20)
    combined = _find(flat, "会计政策、会计估计变更或重大会计差错核算更正的情况说明", 20)
    policy = _find(flat, "会计政策、会计估计变更原因及影响的分析说明", 20)
    error_on = error and _checked(error[1], ("不适用", "适用")) == "适用"
    combined_on = combined and _checked(combined[1], ("不适用", "适用")) == "适用"
    policy_on = policy and _checked(policy[1], ("不适用", "适用")) == "适用"
    if restated is None and not (error or combined or policy):
        return _missing("accounting")
    notes = []
    if policy_on:
        notes.append(f"有会计政策或会计估计变更（多为执行新准则），见第 {policy[0]} 页")
    if error_on:
        return _item("accounting", RED, "更正了重大会计差错",
                     "公司更正了以前年度的重大会计差错，也就是说以前公布的数字有错。", error[0],
                     "；".join(notes))
    if restated == "是":
        why = f"（原因：{reason}）" if reason else ""
        return _item("accounting", YELLOW, "重述了以前年度数据",
                     f"公司调整或重述了以前年度的财务数据{why}，跨年比较时要用调整后的数。",
                     page, "；".join(notes))
    if combined_on:
        return _item("accounting", YELLOW, "有变更或更正",
                     "公司本年有会计政策、会计估计变更或差错更正，报告没有在这里区分是哪一种，建议看原页。",
                     combined[0], "；".join(notes))
    return _item("accounting", GREEN, "无重述",
                 "没有重述以前年度数据，也没有更正重大会计差错。",
                 page or (error or combined or policy)[0], "；".join(notes))


# ---------------------------------------------------------------- 现金流 / 利润含金量

def check_cash(fin: dict | None, period: str) -> dict:
    if not fin or not fin.get("years"):
        return _missing("cash")
    y = fin["years"][0]
    ocf, np_ = _fin_value(fin, "ocf", y), _fin_value(fin, "np_parent", y)
    if ocf is None or np_ is None:
        return _missing("cash")
    page = fin.get("page")
    detail = ""
    if period == FY:
        ocfs = [_fin_value(fin, "ocf", yy) for yy in fin["years"]]
        nps = [_fin_value(fin, "np_parent", yy) for yy in fin["years"]]
        if None not in ocfs and None not in nps:
            detail = (f"近三年经营现金流合计 {_yi(sum(ocfs))}，"
                      f"归母净利润合计 {_yi(sum(nps))}")
    if np_ <= 0:
        status = RED if ocf < 0 else YELLOW
        value = "亏损"
        summary = (f"归母净利润亏损 {_yi(-np_)}，经营现金流也是负的（{_yi(ocf)}），主业在持续消耗现金。"
                   if ocf < 0 else
                   f"归母净利润亏损 {_yi(-np_)}，但经营现金流为正（{_yi(ocf)}）。")
    else:
        r = ocf / np_
        status = RED if r < 0 else YELLOW if r < 0.8 else GREEN
        value = f"{r:.2f}"
        if r < 0:
            summary = (f"账面赚了 {_yi(np_)}，经营现金流却是 {_yi(ocf)}：利润没有变成现金，"
                       "钱可能压在存货或应收账款上。")
        elif r < 0.8:
            summary = f"每 1 元利润只带来 {r:.2f} 元经营现金，利润的现金含量偏低。"
        else:
            summary = f"每 1 元利润带来 {r:.2f} 元经营现金，利润基本是真金白银。"
    if period != FY:
        if status == RED:
            status = YELLOW
        summary += " 季度现金流波动大，以年报为准。"
    return _item("cash", status, value, summary, page, detail)


_GOV_LABEL = "计入当期损益的政府补助"


def _gov_subsidy(pages_lines) -> tuple[float, int] | None:
    """非经常性损益表里政府补助本年金额（元）。标签可能折行，金额在标签之后的第一个数。"""
    pat = re.compile(r"\s*".join(map(re.escape, _GOV_LABEL)))
    for i, lines in enumerate(pages_lines[:25]):
        text = "\n".join(lines)
        if "非经常性损益" not in text:
            continue
        m = pat.search(text)
        if not m:
            continue
        nums = _amounts(text[m.end():m.end() + 300])
        if not nums:
            continue
        upto = text[:m.start()].count("\n")
        unit = _unit_before(lines, upto) or (
            _unit_before(pages_lines[i - 1], len(pages_lines[i - 1])) if i else None) or "元"
        return nums[0] * UNIT_SCALE[unit], i + 1
    return None


def check_profit(fin: dict | None, pages_lines, period: str) -> dict:
    if not fin or not fin.get("years"):
        return _missing("profit")
    y = fin["years"][0]
    np_, ded = _fin_value(fin, "np_parent", y), _fin_value(fin, "np_deducted", y)
    if np_ is None or ded is None:
        return _missing("profit")
    page = fin.get("page")
    gov = _gov_subsidy(pages_lines) if period == FY else None
    detail = ""
    if np_ <= 0:
        if ded > 0:
            return _item("profit", YELLOW, "亏损",
                         f"归母净利润亏损 {_yi(-np_)}，但扣除非经常性损益后盈利 {_yi(ded)}："
                         "主业在赚钱，亏损来自理财、资产处置等非经常性项目。", page)
        return _item("profit", RED, "亏损",
                     f"归母净利润亏损 {_yi(-np_)}，扣除非经常性损益后亏损 {_yi(-ded)}，主业本身在亏钱。", page)
    share = (np_ - ded) / np_ * 100
    if gov:
        detail = f"其中政府补助 {_yi(gov[0])}，占归母净利润 {_pct(gov[0] / np_ * 100)}（第 {gov[1]} 页）"
    if share < 0:
        status = GREEN
        summary = (f"非经常性项目合计是亏的，扣非净利润（{_yi(ded)}）反而高于归母净利润（{_yi(np_)}），"
                   "主业比账面利润更好。")
    else:
        status = _grade(share, 20, 50)
        if status == RED:
            summary = (f"归母净利润 {_yi(np_)}里有 {share:.0f}% 来自非经常性项目（如理财、卖资产、补助），"
                       f"扣掉后只剩 {_yi(ded)}，主业赚得不多。")
        elif status == YELLOW:
            summary = f"归母净利润里有 {share:.0f}% 来自非经常性项目，扣非后为 {_yi(ded)}。"
        else:
            summary = f"非经常性项目只占归母净利润的 {share:.0f}%，利润主要来自主业。"
    if gov and gov[0] / np_ >= 0.3 and status == GREEN:
        status = YELLOW
        summary += " 不过政府补助占净利润的比例较高。"
    return _item("profit", status, _pct(share), summary, page, detail)


# ---------------------------------------------------------------- 前五名客户 / 供应商

_TOP5 = {
    # 键: (上交所句子, 深交所表格)
    "cust": (r"前五名客户销售额([\d,.]+)万?元，占年度销售总额([\d.]+)%",
             r"前五名客户合计销售金额占年度销售总额比例([\d.]+)%"),
    "cust_rel": (r"前五名客户销售额中关联方销售额([\d,.]+)万?元，占年度销售总额([\d.]+)",
                 r"前五名客户销售额中关联方销售额占年度销售总额比例([\d.]+)%"),
    "supp": (r"前五名供应商采购额([\d,.]+)万?元，占年度采购总额([\d.]+)%",
             r"前五名供应商合计采购金额占年度采购总额比例([\d.]+)%"),
    "supp_rel": (r"前五名供应商采购额中关联方采购额([\d,.]+)万?元，占年度采购总额([\d.]+)",
                 r"前五名供应商采购额中关联方采购额占年度采购总额比例([\d.]+)%"),
}


def top5_ratios(flat) -> dict[str, tuple[float, int]]:
    """{cust/cust_rel/supp/supp_rel: (百分比, 页码)}。"""
    out = {}
    for key, (sse, szse) in _TOP5.items():
        for i, text in enumerate(flat):
            m = re.search(sse, text)
            if m:
                out[key] = (float(m.group(2).rstrip(".")), i + 1)
                break
            m = re.search(szse, text)
            if m:
                out[key] = (float(m.group(1).rstrip(".")), i + 1)
                break
    return out


def check_top5(flat) -> list[dict]:
    r = top5_ratios(flat)
    disclosed = any("前五名客户" in t or "前5名客户" in t for t in flat)
    why = "未找到" if disclosed else "报告没有披露前五名客户和供应商，不作判断。"
    items = []
    for key, k, who, base in (("customers", "cust", "客户", "销售额"),
                              ("suppliers", "supp", "供应商", "采购额")):
        if k not in r:
            items.append(_missing(key, why) if disclosed else _item(key, NONE, "未披露", why))
            continue
        v, page = r[k]
        status = _grade(v, 50, 80)
        if status == GREEN:
            summary = f"前五名{who}合计占{base}的 {_pct(v)}，没有过度依赖少数{who}。"
        else:
            summary = (f"前五名{who}合计占{base}的 {_pct(v)}，{'超过八成' if status == RED else '超过一半'}"
                       f"集中在少数{who}，一旦其中一家变化影响很大。"
                       "（有些行业天然集中，比如医药流通、消费电子代工、广告投放，要结合行业看。）")
        items.append(_item(key, status, _pct(v), summary, page))
    rel = [(r[k][0], r[k][1], who) for k, who in (("cust_rel", "客户"), ("supp_rel", "供应商")) if k in r]
    if not rel:
        items.append(_missing("related", why) if disclosed else _item("related", NONE, "未披露", why))
    else:
        v, page, who = max(rel)
        status = _grade(v, 10, 30)
        parts = "，".join(f"前五名{w}中关联方占 {_pct(x)}" for x, _, w in rel)
        if status == GREEN:
            summary = f"{parts}，关联交易占比不高。"
        else:
            summary = f"{parts}：有相当一部分生意是和关联方做的，要留意定价是否公允。"
        items.append(_item("related", status, _pct(v), summary, page,
                           "只统计前五名客户和供应商中的关联方，不是公司全部关联交易。"))
    return items


# ---------------------------------------------------------------- 商誉

# 「商誉」前后都不是汉字才是报表科目（排除「负商誉」「商誉减值」）；后面直接跟汉字 = 空行粘上了下一个科目
_GW_RE = re.compile(rf"(?<![{_CJK}])商誉")
# 附注编号：「（七）27」「(五)18」「七、26」
_NOTE_RE = re.compile(r"[(（][一二三四五六七八九十]+[)）]\s*\d*|[一二三四五六七八九十]+、\d+")
# 报表标题：「合并资产负债表」，深交所写「1、合并资产负债表」。目录里的「合并资产负债表 7 - 8」不算。
_BS_RE = re.compile(r"^(?:\d+[、.])?合并资产负债表$")


def goodwill(pages_lines) -> tuple[float, int, bool] | None:
    """合并资产负债表里的商誉期末数 → (元, 页码, 报表里有没有这一行)。

    · 行是空的（只有「商誉」两个字），或粘上了下一个科目（七匹狼三季报「商誉长期待摊费用」）= 0
    · 恒瑞干脆没有这一行：已经读到「无形资产」「长期待摊费用」还没见到商誉，也按 0
    """
    start = None
    for i, lines in enumerate(pages_lines):
        if any(_BS_RE.match(re.sub(r"\s+", "", ln)) for ln in lines):
            start = i
            break
    if start is None:
        return None
    seen_noncurrent = False
    for i in range(start, min(start + 3, len(pages_lines))):
        lines = pages_lines[i]
        for li, ln in enumerate(lines):
            s = ln.strip()
            m = _GW_RE.search(s)
            if m:
                rest = _NOTE_RE.sub(" ", s[m.end():])
                head = re.split(f"[{_CJK}]", rest, maxsplit=1)[0]
                nums = _amounts(head)
                unit = _unit_before(lines, li) or (
                    _unit_before(pages_lines[start], len(pages_lines[start])) if i > start else None) or "元"
                return (nums[0] * UNIT_SCALE[unit] if nums else 0.0), i + 1, True
            if "无形资产" in s or "长期待摊费用" in s:
                seen_noncurrent = True
    return (0.0, start + 1, False) if seen_noncurrent else None


_EQUITY_RE = re.compile(r"^归属于母公司(?:所有者|股东)权益(?:[(（]或股东权益[)）])?合计")


def parent_equity(pages_lines, start_page: int) -> float | None:
    """合并资产负债表里的「归属于母公司所有者权益合计」（元）。主要会计数据表抽不到时兜底
    （蓝色光标重述后那张表是调整前/调整后六列、数字还折行）。"""
    for i in range(start_page - 1, min(start_page + 3, len(pages_lines))):
        lines = pages_lines[i]
        for li, ln in enumerate(lines):
            s = re.sub(r"\s+", "", ln[:30]) + ln[30:]
            m = _EQUITY_RE.match(s)
            if m:
                nums = _amounts(s[m.end():])
                if nums:
                    unit = _unit_before(lines, li) or "元"
                    return nums[0] * UNIT_SCALE[unit]
    return None


def check_goodwill(fin: dict | None, pages_lines) -> dict:
    gw = goodwill(pages_lines)
    if gw is None:
        return _missing("goodwill")
    amount, page, listed = gw
    if amount == 0:
        return _item("goodwill", GREEN, "无", "合并报表里没有商誉，不存在商誉减值的风险。", page,
                     "" if listed else "合并资产负债表里没有列「商誉」这一行。")
    equity = _fin_value(fin, "equity_parent", fin["years"][0]) if fin and fin.get("years") else None
    if equity is None:
        equity = parent_equity(pages_lines, page)
    if not equity:
        return _item("goodwill", NONE, _yi(amount), f"商誉 {_yi(amount)}，但没抽到净资产，无法算占比。", page)
    if equity <= 0:
        return _item("goodwill", RED, _yi(amount), f"商誉 {_yi(amount)}，而归母净资产已经为负。", page)
    r = amount / equity * 100
    status = _grade(r, 10, 30)
    if status == GREEN:
        summary = f"商誉 {_yi(amount)}，只占归母净资产的 {_pct(r)}，就算减值影响也有限。"
    else:
        summary = (f"商誉 {_yi(amount)}，占归母净资产的 {_pct(r)}。商誉来自以前的高价收购，"
                   "被收购的业务一旦不及预期就要减值，直接冲减利润。")
    return _item("goodwill", status, _pct(r), summary, page)


# ---------------------------------------------------------------- 大股东质押

def check_pledge(flat) -> dict:
    hit = _find(flat, "累计质押股份数量占其所持公司股份数量比例", 30)
    if not hit:
        return _missing("pledge")
    page, tail = hit
    opt = _checked(tail, ("不适用", "适用"))
    if opt is None:
        return _missing("pledge")
    if opt == "适用":
        return _item("pledge", RED, "超过 80%",
                     "控股股东或第一大股东质押了所持股份的八成以上。股价大跌时可能被强制平仓，控制权也可能不稳。",
                     page)
    return _item("pledge", GREEN, "未超 80%", "控股股东质押比例没有超过 80%。", page)


# ---------------------------------------------------------------- 汇总

def build_from_lines(pages_lines: list[list[str]], fin: dict | None, period: str = FY) -> dict:
    flat = _flat(pages_lines)
    items: dict[str, dict] = {}
    if period == FY:
        items["audit"] = check_audit(pages_lines, flat)
        items["accounting"] = check_accounting(flat)
    items["cash"] = check_cash(fin, period)
    items["profit"] = check_profit(fin, pages_lines, period)
    if period == FY:
        for it in check_top5(flat):
            items[it["key"]] = it
    items["goodwill"] = check_goodwill(fin, pages_lines)
    if period == FY:
        items["pledge"] = check_pledge(flat)
    for key in ANNUAL_ONLY:
        items.setdefault(key, _item(key, NONE, "仅年报披露", "季报和半年报不披露这一项，以年报为准。"))
    ordered = [items[k] for k in ORDER]
    counts = {s: sum(1 for it in ordered if it["status"] == s) for s in (RED, YELLOW, GREEN, NONE)}
    return {"version": HC_VERSION, "period": period, "items": ordered, "counts": counts}


# ---------------------------------------------------------------- 回合与卡片
#
# 体检回合不检索、不调模型：卡片和摘要都是程序按上面的结果写的。
# 「年报提示了哪些经营风险」这类问管理层自述风险的，仍走正常回答（不在下面的词里）。

_WANTS_RE = re.compile(r"体检|排雷|避雷|踩雷|有没有雷|隐患|红黄绿|财务风险|风险点|"
                       r"有什么风险|有哪些风险|有没有风险|健康状况|财务健康")
_STATUS_NAME = {RED: "红灯", YELLOW: "黄灯", GREEN: "绿灯"}


def wants_health(question: str) -> bool:
    return bool(_WANTS_RE.search(question or ""))


def button_question(company: str) -> str:
    """「体检」按钮发出的问题。写成自然语言，聊天记录里读得懂，也能被 wants_health 认出。"""
    who = company or "这家公司"
    return f"给{who}做个年报体检：审计意见、利润和现金流含金量、客户集中度、商誉、大股东质押"


def compact(hc: dict) -> dict:
    """存进历史和浏览器的形状：去掉判断标准（固定文字，下发时按 key 从 RULES 补回）。"""
    keep = ("key", "label", "status", "value", "summary", "detail", "page")
    return {"period": hc.get("period", FY), "counts": dict(hc.get("counts") or {}),
            "items": [{k: it.get(k) for k in keep} for it in hc.get("items") or []]}


def brief(hc: dict, label: str) -> str:
    """回答正文：几红几黄、各是哪几项、在第几页。细节在卡片里。"""
    items = hc.get("items") or []
    judged = [it for it in items if it["status"] != NONE]
    c = {s: sum(1 for it in judged if it["status"] == s) for s in (RED, YELLOW, GREEN)}
    src = f"《{label}》" if label else "这份报告"
    head = (f"按{src}的披露做了 {len(judged)} 项体检：**红灯 {c[RED]} 项，"
            f"黄灯 {c[YELLOW]} 项，绿灯 {c[GREEN]} 项**。")
    skipped = len(items) - len(judged)
    if skipped:
        head += f"另有 {skipped} 项{'只在年报里披露' if hc.get('period', FY) != FY else '没有找到披露'}，未作判断。"
    paras = [head]
    for status in (RED, YELLOW):
        hits = [it for it in judged if it["status"] == status]
        if hits:
            paras.append(f"**{_STATUS_NAME[status]}**：" + "；".join(
                f"{it['label']} {it['value']}" + (f"（第 {it['page']} 页）" if it.get("page") else "")
                for it in hits) + "。")
    if not c[RED] and not c[YELLOW] and judged:
        paras.append("没有亮红灯或黄灯的项目。")
    paras.append("每一项的判断标准写在卡片里，点开可以翻到原页核对。体检只看报告披露的数字和勾选项，"
                 "不构成投资建议；想了解管理层自己列出的经营风险，可以问「年报提示了哪些经营风险？」")
    return "\n\n".join(paras)


def build(data: bytes, fin: dict | None, period: str = FY) -> dict:
    from .pdf_parser import raw_page_lines

    return build_from_lines(raw_page_lines(data, max_pages=10 ** 4), fin, period)


def load_or_build(doc_id: str, fin: dict | None) -> dict | None:
    """读 data/<doc_id>/healthcheck.json；没有或版本不对就从 source.pdf 现做并写回。"""
    folder = config.DATA_DIR / doc_id
    path = folder / HC_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("version") == HC_VERSION:
            return cached
    except (OSError, ValueError):
        pass
    pdf = folder / "source.pdf"
    if not pdf.exists():
        return None
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    try:
        hc = build(pdf.read_bytes(), fin, period=meta.get("period") or FY)
    except Exception:
        return None
    try:
        path.write_text(json.dumps(hc, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return hc
