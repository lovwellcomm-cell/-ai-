"""定期报告的「期」：年报、一季报、半年报、三季报。

一份文档由「公司 + 年份 + 期」确定。老文档的 meta.json 里没有 period，一律按年报（FY）理解，
所以年报的行为和加季报之前完全一样。

这里只放纯函数：问题里问的是哪一期、这一期叫什么、最近一期大概是哪一年。
"""
from __future__ import annotations

import re
from datetime import date

FY, Q1, H1, Q3 = "FY", "Q1", "H1", "Q3"
PERIODS = (Q1, H1, Q3, FY)

# 简称（文档名、提示语）
SHORT = {FY: "年报", Q1: "一季报", H1: "半年报", Q3: "三季报"}
# 公告标题里的写法：「2026年第三季度报告」
TITLE = {FY: "年度报告", Q1: "第一季度报告", H1: "半年度报告", Q3: "第三季度报告"}
# 「年初至报告期末」覆盖的范围
SPAN = {FY: "全年", Q1: "一季度", H1: "上半年", Q3: "前三季度"}
MONTHS = {FY: "1–12月", Q1: "1–3月", H1: "1–6月", Q3: "1–9月"}
# 同一年里几份报告的完整程度：同一年有多份时优先年报
RANK = {Q1: 1, H1: 2, Q3: 3, FY: 4}
# 法定披露截止日（月, 日）；年报在次年
_DEADLINE = {Q1: (4, 30), H1: (8, 31), Q3: (10, 31), FY: (4, 30)}

# 顺序有讲究：「前三季度」「第三季度」要先于「一季度」之类的判断；
# 「二季度」只有半年报能回答（半年报的本报告期就是 1–6 月，含二季度）；
# 「四季度」只有年报的「分季度主要财务数据」里有。
_PATTERNS = (
    (Q3, r"前三季度|第?三季度|三季报|[Qq]3|1\s*[-–—~～至到]\s*9\s*月"),
    (H1, r"半年报|半年度|中报|中期报告|上半年|第?二季度|[Qq]2|[Hh]1|1\s*[-–—~～至到]\s*6\s*月"),
    (Q1, r"第?一季度|一季报|[Qq]1|1\s*[-–—~～至到]\s*3\s*月"),
    (FY, r"第?四季度|[Qq]4|全年|\d{4}\s*年?\s*(?:年报|年度报告|年度)|(?:今年|去年|前年)的?(?:年报|年度报告)"),
)
_GENERIC_Q_RE = re.compile(r"季报|季度报告")
# 「最新财报怎么样」「最近一期业绩」：没说哪一期，四种报告里取最新披露的那份。
# 只认「最新/最近 + 报告类名词」：「行业最新动态」「最近行业有什么变化」不是在要报告。
_LATEST_RE = re.compile(
    r"(最新|最近)\s*(一[期份]|的)?\s*(财报|报告|定期报告|财务报告|财务报表|报表|业绩|财务数据|一期|一份)")
_LATEST_FY_RE = re.compile(r"(最新|最近)\s*(一[期份]|的)?\s*(年报|年度报告)")
_END_MONTH = {Q1: 3, H1: 6, Q3: 9, FY: 12}
_REL_YEAR_RE = re.compile(
    r"(今年|去年|前年)的?\s*(?=第?[一二三四]季度|[一三]季报|半年|中报|上半年|前三季度|全年|年报|年度报告)")
_REL = {"今年": 0, "去年": 1, "前年": 2}


def period_of(meta: dict | None) -> str:
    p = (meta or {}).get("period") or FY
    return p if p in PERIODS else FY


def period_in(question: str) -> str | None:
    """问题里明确问的是哪一期；没提就是 None（由调用方按当前文档理解）。

    「年报」只在和年份连用时算数（「2025年报」「去年年报」）—— 这是个年报助手，
    用户顺口说「这份年报里」并不是在要求从季报换成年报。
    """
    q = question or ""
    for period, pat in _PATTERNS:
        if re.search(pat, q):
            return period
    return None


def wants_latest_quarter(question: str) -> bool:
    """「最新季报」：没说哪一季，取已经披露的最近一期季报。"""
    return bool(_GENERIC_Q_RE.search(question or "")) and period_in(question) is None


def wants_latest(question: str) -> str | None:
    """「最新财报」→ "any"（四种报告都算）；「最新年报」→ FY；没这么问 → None。
    明确说了哪一期（「最新三季报」）的交给 period_in，这里不管。"""
    q = question or ""
    if period_in(q) is not None:
        return None
    if _LATEST_FY_RE.search(q):
        return FY
    if _LATEST_RE.search(q):
        return "any"
    return None


def relative_year(question: str, today: date | None = None) -> int | None:
    """「去年三季度」「今年一季报」里的相对年份。

    只在紧跟着某一期的说法时才换算：「去年同期」是报告里的比较口径，不是在问另一年。
    """
    m = _REL_YEAR_RE.search(question or "")
    if not m:
        return None
    return (today or date.today()).year - _REL[m.group(1)]


def deadline(period: str, year: int) -> date:
    month, day = _DEADLINE[period]
    return date(year + 1 if period == FY else year, month, day)


def latest_year(period: str, today: date | None = None) -> int:
    """这一期已经过了披露截止日的最近一年。截止前多数公司已经披露，但不能保证。"""
    today = today or date.today()
    y = today.year
    while deadline(period, y) > today:
        y -= 1
    return y


def latest_quarter(today: date | None = None) -> tuple[int, str]:
    """已经过了截止日的最近一期季报（不含年报）。"""
    today = today or date.today()
    best = max(((latest_year(p, today), p) for p in (Q1, H1, Q3)),
               key=lambda t: (t[0], RANK[t[1]]))
    return best


def latest_report(today: date | None = None) -> tuple[int, str]:
    """已经过了截止日的最近一期报告（四种都算），按报告期末的先后比：
    2026-05-01 时 2025 年报和 2026 一季报都刚过截止日，取报告期更晚的一季报。"""
    today = today or date.today()
    return max(((latest_year(p, today), p) for p in PERIODS),
               key=lambda t: (t[0], RANK[t[1]]))


def period_end(year: int, period: str) -> date:
    month = _END_MONTH[period]
    return date(year, month, 31 if month in (3, 12) else 30)


def next_report(year: int, period: str) -> tuple[int, str]:
    """紧接着的下一期：三季报之后是同年年报，年报之后是次年一季报。"""
    order = (Q1, H1, Q3, FY)
    i = order.index(period)
    return (year + 1, Q1) if period == FY else (year, order[i + 1])


def report_label(year: str | int, period: str) -> str:
    """「2026 三季报」「2025 年报」"""
    return f"{year} {SHORT[period]}".strip() if year else SHORT[period]


def doc_desc(company: str, year: str, period: str = FY) -> str:
    """给模型的文档说明。季报要讲清楚「本报告期」指什么、未经审计。"""
    if not (company or year):
        return ""
    period = period if period in PERIODS else FY
    head = f"{company} {year} 年{TITLE[period]}".strip()
    if period == FY:
        return f"（{head}）"
    if period == Q3:
        scope = "「本报告期」指第三季度单季（7–9月），「年初至报告期末」指 1–9 月"
    elif period == H1:
        scope = "「本报告期」指 1–6 月"
    else:
        scope = "「本报告期」指第一季度（1–3月）"
    return f"（{head}；{scope}；季度和半年度财务数据通常未经审计）"


def detect(text: str) -> tuple[str, str]:
    """从封面文字识别 (年份, 期)。「2026 年第三季度报告」→ ("2026", "Q3")。"""
    t = text or ""
    for period in (Q1, H1, Q3):
        m = re.search(r"(20\d{2})\s*年\s*" + TITLE[period], t)
        if m:
            return m.group(1), period
    m = re.search(r"(20\d{2})\s*年\s*年度报告", t)
    if m:
        return m.group(1), FY
    return "", FY
