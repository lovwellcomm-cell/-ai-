"""结构化财务数据：从定期报告的「主要会计数据」表里确定性地抽出关键数字。

年报抽近三年；季报和半年报抽本期与上年同期（见文件后半部分）。

为什么单独抽这张表：每份 A 股年报都有它，格式高度统一（本年、上年、增减、前年四列），
而且公司**自己算好了增减比例** —— 我们复算一遍和它对得上，才算抽对了。
抽不准就不展示，宁可少一张卡，也不在投资者面前摆一个错数字。

数字由这里抽、比率由 ui/card.py 用 Python 算；LLM 不参与，所以不存在「模型抄错 / 算错」。

几个真实年报里踩到的格式差异（测试夹具就是这些原文）：
  · 上交所：`营业收入 24,673,670,269.16 22,322,579,930.98 10.53 17,905,865,235.94`
  · 深交所：增减带 %，单位写在科目里 `营业收入（元） 3,003,522,655.39 … -4.35% …`
  · 长科目折行：数字单独成行，或科目后半截与数字同行（`利润 86,228,146,421.62 …`）
  · 单位：时代新材这张表是「万元」，而它的报表是「元」——必须按表判断
  · 贵州茅台的「单位：元」被页眉过滤删掉了，所以这里读的是原始页面文本
  · 恒瑞、牧原、立讯：扣非净利润的科目折成三行（`归属于上市公司股东的` / `扣除非经常性损益的净` / `利润`）
  · 精达 2022：列更窄，扣非科目折成四行（`归属于上市公司` / `股东的扣除非经` / `常性损益的净利` / `润`）
  · 万科：负数写成 `(88,556,470,495.64)`，扣非写「扣除非经常性损益后归属于上市公司股东的净利润」，
    总资产写「资产总额」
"""
from __future__ import annotations

import json
import re

from . import config
from .periods import FY, Q3, detect

# 2：加入季报/半年报（period 字段）。老缓存版本不符，第一次用到时自动重抽。
# 3：三行科目、括号负数、万科的别名 —— 以前漏抽的扣非净利润能补上。
# 4：追溯调整后的「调整后 / 调整前」5–6 列格式（长江电力）。
# 5：三季报利润表科目折行、本期和上年同期金额各占一行（精达 2025 三季报以前整张表抽不出）。
# 6：年报科目折四行（精达 2022 的扣非净利润以前漏抽）；数字本身折行（蓝色光标 2025 整张表以前抽不出）。
# 7：季报、半年报的数字折行也接上（样本里还没遇到，提前补）。
FIN_VERSION = 7
FIN_FILE = "financials.json"

UNIT_SCALE = {"亿元": 1e8, "万元": 1e4, "千元": 1e3, "元": 1.0}

# 科目键 → 年报里的写法（去掉空格和「（元）」之后精确比对）
ITEMS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入",),
    "total_profit": ("利润总额",),
    "np_parent": ("归属于上市公司股东的净利润",),
    "np_deducted": ("归属于上市公司股东的扣除非经常性损益的净利润",
                    "扣除非经常性损益后归属于上市公司股东的净利润"),
    "ocf": ("经营活动产生的现金流量净额",),
    # 季报写「所有者权益」，年报和半年报写「净资产」
    "equity_parent": ("归属于上市公司股东的净资产", "归属于上市公司股东的所有者权益"),
    "total_assets": ("总资产", "资产总额"),
}
LABELS = {k: v[0] for k, v in ITEMS.items()}
FLOW_KEYS = ("revenue", "total_profit", "np_parent", "np_deducted", "ocf")
BALANCE_KEYS = ("equity_parent", "total_assets")
_ALIAS = {alias: key for key, aliases in ITEMS.items() for alias in aliases}

_NUM_RE = re.compile(r"\(-?\d[\d,]*\.\d+\)|-?\d[\d,]*\.\d+%?")
# 数字折行拼接用：一个完整的数（千分位要么规范、要么没有），一行只有数字碎片
_AMOUNT = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+"
_WELL_RE = re.compile(rf"^(?:\({_AMOUNT}\)|{_AMOUNT}%?)$")
_FRAG_RE = re.compile(r"^[\d,.\-%()\s]+$")
_YEAR_RE = re.compile(r"(20\d\d)\s*年")
_UNIT_DECL_RE = re.compile(r"单位\s*[:：]\s*(亿元|万元|千元|元)")
_UNIT_IN_LABEL_RE = re.compile(r"[（(](亿元|万元|千元|元)[)）]")
# 表的结束：下一张「主要财务指标」表，或下一个一级标题（「八、」）
_END_RE = re.compile(r"^\s*([(（]二[)）]|主要财务指标|[一二三四五六七八九十]{1,3}、)")
_WINDOW = 60          # 从标题往下最多看多少行
_TOLERANCE = 0.1      # 复算增减与披露值允许相差的百分点
_ROUNDING = 0.0051    # 两位小数四舍五入的误差（调整前后两列都在容差内时用来分辨）


def _norm(label: str) -> str:
    return _UNIT_IN_LABEL_RE.sub("", re.sub(r"\s+", "", label))


def _to_float(raw: str) -> float:
    if raw.startswith("(") and raw.endswith(")"):
        return -float(raw[1:-1].replace(",", ""))
    return float(raw.replace(",", "").rstrip("%"))


def detect_unit(lines: list[str]) -> str:
    """表头前后的「单位：万元」优先；没有就看科目里的「（元）」；都没有按「元」。"""
    for ln in lines:
        m = _UNIT_DECL_RE.search(ln)
        if m:
            return m.group(1)
    for ln in lines:
        m = _UNIT_IN_LABEL_RE.search(ln)
        if m:
            return m.group(1)
    return "元"


def _match_label(buffer: list[str], head: str) -> str | None:
    """科目可能折成两到五行。依次试「本行开头」「上一行 + 本行开头」……最多往上拼四行。"""
    for k in range(0, min(4, len(buffer)) + 1):
        parts = buffer[len(buffer) - k:] if k else []
        key = _ALIAS.get(_norm("".join(parts) + head))
        if key:
            return key
    return None


def _restated(vals: list[float]) -> list[float] | None:
    """上年数据追溯调整过时，上年（和前年）拆成「调整后、调整前」两列：一行 5 个或 6 个数。
    两家的列序还不一样（长江电力「调整后 调整前」，蓝色光标「调整前 调整后」），
    所以两种都试，用披露的增减比例复算，对得上的那种才算：[本年, 上年, 增减%, 前年]。"""
    if len(vals) == 5:
        cands = [[vals[0], vals[1], vals[3], vals[4]], [vals[0], vals[2], vals[3], vals[4]]]
    elif len(vals) == 6:
        cands = [[vals[0], vals[1], vals[3], vals[4]], [vals[0], vals[2], vals[3], vals[5]]]
    else:
        return None
    ok = [c for c in cands if verify(c)]
    # 调整前后数字相同（长江电力的利润表科目就是）两种都对：取哪个都一样
    if ok and all(c[1] == ok[0][1] for c in ok):
        return ok[0]
    # 调整前后只差一点（长江电力的净资产、总资产差 0.03%），两种都落在容差里：
    # 披露的增减精确到两位小数，按四舍五入精度只有一种对得上才算
    exact = [c for c in ok if verify(c, tolerance=_ROUNDING)]
    return exact[0] if len(exact) == 1 else None


def _row_values(nums: list[str]) -> list[float] | None:
    """一行的数字 → [本年, 上年, 增减%, 前年]；个数不对或复算对不上返回 None。"""
    vals = [_to_float(n) for n in nums]
    if len(vals) == 4:
        return vals
    return _restated(vals) if len(vals) in (5, 6) else None


def _wrapped_row(ln: str, nexts: list[str]) -> tuple[str, list[float], int] | None:
    """列太窄时数字本身也会折行（蓝色光标 2025）：`430.4` / `8`、`156` / `.95`、`971.` / `13`，
    负号单独留在行尾 `777.93 -` / `290,675,602.27`。把后面的纯数字行接上来再认。

    每个断点要么直接拼上（同一个数），要么是两个数之间的空格，两种都试；只认
    「每个数的千分位格式都对、金额小数位一致、个数 4–6、用披露的增减复算对得上」且结果唯一的那种接法。
    返回 (科目文字, 数值, 用掉的续行数)。
    """
    first = _NUM_RE.search(ln) or re.search(r"\s-$", ln)
    if not first:
        return None
    head, tail = ln[: first.start()].strip(), ln[first.start():].strip()
    for k in range(1, min(3, len(nexts)) + 1):
        conts = [x.strip() for x in nexts[:k]]
        if not all(_FRAG_RE.match(c) for c in conts):
            return None                        # 续行带文字：是下一个科目，不是数字的后半截
        found = set()
        for mask in range(1 << k):
            text = tail
            for i, c in enumerate(conts):
                text += (" " if mask >> i & 1 else "") + c
            toks = text.split()
            if not all(_WELL_RE.match(t) for t in toks):
                continue
            # 同一行的金额小数位一致：`430.4` + `860,796,…` 这种接法复算也可能凑巧对上
            # （差额落在不用的那列「调整前」里），但小数位露了馅
            if len({len(t.rstrip(")").split(".")[1]) for t in toks if not t.endswith("%")}) > 1:
                continue
            vals = _row_values(toks)
            if vals and verify(vals):
                found.add(tuple(vals))
        if len(found) == 1:
            return head, list(found.pop()), k
        if found:
            return None                        # 几种接法都说得通：不猜
    return None


def parse_rows(lines: list[str]) -> dict[str, list[float]]:
    """逐行扫描，返回 {科目键: [本年, 上年, 增减%, 前年]}（原始单位，未换算）。

    认**恰好 4 个数**的行：本表的列序固定为本年、上年、增减、前年。
    5–6 个数的行是追溯调整后的格式，交给 _restated 用复算决定列序。
    数字个数不对时先看是不是数字折行了（_wrapped_row，接上后必须复算对得上）；
    还不对（比如「不适用」）就无法确定列的归属，直接跳过，而不是去猜。
    """
    rows: dict[str, list[float]] = {}
    buffer: list[str] = []
    lines = [x.strip() for x in lines if x.strip()]
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        nums = _NUM_RE.findall(ln)
        vals = _row_values(nums)
        head = ln[: ln.find(nums[0])].strip() if vals else ""
        if not vals and (nums or ln.endswith(" -")):
            wrapped = _wrapped_row(ln, lines[i:i + 3])
            if wrapped:
                head, vals, used = wrapped
                i += used
        if vals:
            key = _match_label(buffer, head)
            if key and key not in rows:
                rows[key] = vals
            buffer = []
            continue
        if nums or _YEAR_RE.search(ln):
            # 年份表头、或数字个数不对的行：都是边界，前面累积的半截科目作废。
            buffer = []
            continue
        buffer = (buffer + [ln])[-4:]
    return rows


def verify(values: list[float], tolerance: float = _TOLERANCE) -> bool:
    """用本年、上年复算增减，与公司披露的比例对比。对不上 = 列错位或科目抽错。"""
    cur, prior, change, _ = values
    if prior == 0:
        return False
    return abs((cur - prior) / abs(prior) * 100 - change) <= tolerance


def _table_windows(pages_lines: list[list[str]]):
    """逐个候选位置产出窗口：[(页码, 行), …]。表可能跨页，所以每行带着自己的页码。

    目录页里也会出现「主要会计数据」，所以不能只取第一处 —— 由调用方解析失败后继续往下找。
    """
    for pi, lines in enumerate(pages_lines):
        for li, ln in enumerate(lines):
            if "主要会计数据" not in ln:
                continue
            window = [(pi + 1, x) for x in lines[li:]]
            if len(window) < _WINDOW and pi + 1 < len(pages_lines):
                window += [(pi + 2, x) for x in pages_lines[pi + 1]]
            yield window[:_WINDOW]


def _page_of(window: list[tuple[int, str]], label: str) -> int:
    """「营业收入」那一行所在的页 —— 指标卡上写的页码要能直接翻过去核对。"""
    for page, ln in window:
        if _norm(ln).startswith(label) and _NUM_RE.search(ln):
            return page
    return window[0][0]


def _cut_at_end(window: list[str]) -> list[str]:
    """标题行本身可能就是「七、近三年主要会计数据…」，从第二行起才检查结束标记。"""
    for i, ln in enumerate(window[1:], start=1):
        if _END_RE.match(ln.strip()) and "主要会计数据" not in ln:
            return window[:i]
    return window


def extract_from_lines(pages_lines: list[list[str]]) -> dict | None:
    """在原始页面行里找主要会计数据表并抽取。必须同时抽到营收和归母净利润，否则返回 None。"""
    for window in _table_windows(pages_lines):
        table = _cut_at_end([ln for _, ln in window])
        window = window[:len(table)]
        rows = parse_rows(table)
        if "revenue" not in rows or "np_parent" not in rows:
            continue
        years = [int(y) for y in _YEAR_RE.findall("\n".join(table))]
        if not years:
            continue
        # 表头第一个年份就是本年（「主要会计数据 2025年 2024年」「2025 年 2024 年 本年比上年增减」）
        year = years[0]
        unit = detect_unit(table)
        scale = UNIT_SCALE[unit]
        items = {}
        for key, vals in rows.items():
            if not verify(vals):
                continue
            cur, prior, change, prior2 = vals
            items[key] = {
                "label": LABELS[key],
                "values": {str(year): cur * scale, str(year - 1): prior * scale,
                           str(year - 2): prior2 * scale},
                "change": change,
            }
        if "revenue" not in items or "np_parent" not in items:
            continue
        return {
            "version": FIN_VERSION,
            "period": FY,
            "unit": unit,
            "page": _page_of(window, LABELS["revenue"]),
            "years": [str(year), str(year - 1), str(year - 2)],
            "items": items,
        }
    return None


# ---------------------------------------------------------------- 季报 / 半年报
#
# 和年报不同的地方（夹具是真实季报原文）：
#   · 一季报、半年报：每行 3 个数 —— 本报告期、上年同期、增减。可以像年报一样复算核对。
#     上年数据追溯调整过时「上年同期」拆成调整前 / 调整后两列，变成 4 个数，用调整后那列。
#   · 三季报：每行 4 格 —— 单季、单季增减、年初至今、年初至今增减。**没有上年同期的金额**，
#     没法直接复算；单季的经营现金流写「不适用」或「—」。
#     核对办法：年初至今的金额在后面的利润表 / 现金流量表里会再出现一次，旁边就是上年同期，
#     拿它复算增减，对得上才算数；扣非净利润不在报表里，用「归母净利润 − 非经常性损益合计」核对。
#   · 资产类科目都是「本报告期末、上年度末、增减」3 个数。
#   · 精达半年报的扣非科目被分页截断：「归属于上市公司股东的扣除非经常 319,… 290,… 10.13」，
#     后半截「性损益的净利润」在下一页的页眉、页码之后才出现。

_CELL_RE = re.compile(r"^[-−]?\d[\d,]*(?:\.\d+)?%?$")
_BLANK_CELLS = ("不适用", "—", "——", "--", "－", "/", "-")
_AUDIT_NO_RE = re.compile(r"未经审计|是否(?:已)?经过?审计\s*□\s*是")
_AUDIT_YES_RE = re.compile(r"是否(?:已)?经过?审计\s*[√☑■]\s*是")


def _is_cell(tok: str) -> bool:
    return bool(_CELL_RE.match(tok)) or tok in _BLANK_CELLS


def _cells(line: str) -> tuple[str, list[str]]:
    """一行拆成 (科目文字, 数字格)。数字格可以是「不适用」「—」这种占位。

    从第一个数字格起往后都必须是数字格，否则这一行不是数据行（比如变动原因说明）。
    """
    parts = line.split()
    for i, tok in enumerate(parts):
        if _is_cell(tok):
            tail = parts[i:]
            return ("".join(parts[:i]), tail) if all(_is_cell(t) for t in tail) else (line, [])
    return line, []


def _num(tok: str) -> float | None:
    return _to_float(tok.replace("−", "-")) if _CELL_RE.match(tok) else None


def _alias_prefix(text: str) -> bool:
    """text 是某个科目名的前半截（还没写完）。"""
    t = _norm(text)
    return bool(t) and any(a.startswith(t) and a != t for a in _ALIAS)


_PFRAG_RE = re.compile(r"^[-−\d,.%]+$")
_PCELL_WELL_RE = re.compile(r"^[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$")


def _is_frag(tok: str) -> bool:
    return bool(_PFRAG_RE.match(tok)) or tok in _BLANK_CELLS


def _frag_line(ln: str) -> bool:
    """这一行只有数字碎片和占位符（「8」「.95」「983.53 -6.92%」）：可能是上一行数字的后半截。"""
    parts = ln.split()
    return bool(parts) and all(_is_frag(t) for t in parts) and any(c.isdigit() for c in ln)


def _wrapped_cells(ln: str, nexts: list[str]) -> tuple[str, list[list[str]], int] | None:
    """季报版的数字折行拼接（年报版见 _wrapped_row）：`611,462,915.8` / `0 -6.01% …`、
    `-109.59% -` / `50,553,323.89 …`、`10,060,538,` / `983.53 …`、四格的最后一格掉到下一行。

    三季报这一行在这里没法复算（要到报表里核对），所以只按格式筛：每格是规范的数或占位符、
    金额小数位一致、3–4 格；筛剩的接法都交回去，由 _periodic_item 核对，只有一种对得上才算。
    返回 (科目文字, 候选数字格, 用掉的续行数)。
    """
    parts = ln.split()
    start = next((i for i in range(len(parts)) if all(_is_frag(t) for t in parts[i:])), None)
    if start is None:
        return None
    head, tail = "".join(parts[:start]), " ".join(parts[start:])
    for k in range(1, min(3, len(nexts)) + 1):
        conts = nexts[:k]
        if not all(_frag_line(c) for c in conts):
            break                              # 续行带文字：是下一个科目，不是数字的后半截
        cands: list[list[str]] = []
        for mask in range(1 << k):
            text = tail
            for i, c in enumerate(conts):
                text += (" " if mask >> i & 1 else "") + c
            toks = text.split()
            if not 3 <= len(toks) <= 4:
                continue
            if not all(_PCELL_WELL_RE.match(t) or t in _BLANK_CELLS for t in toks):
                continue
            decimals = {len(t.split(".")[1]) if "." in t else 0
                        for t in toks if t not in _BLANK_CELLS and not t.endswith("%")}
            if len(decimals) <= 1 and toks not in cands:
                cands.append(toks)
        if cands:
            return head, cands, k
    return None


def parse_periodic_rows(lines: list[str],
                        alts: dict[str, list[list[str]]] | None = None) -> dict[str, list[str]]:
    """{科目键: 数字格}。科目最多折 4 行；科目后半截被分页挤到数字后面也能接上。

    数字本身折行时（_wrapped_cells）可能有几种接法：第一种放进返回值，全部候选放进 alts，
    由调用方逐个核对。
    """
    rows: dict[str, list[str]] = {}
    buffer: list[str] = []
    pending: tuple[str, list[str], int] | None = None    # (半截科目, 数字格, 已跳过行数)
    lines = [x.strip() for x in lines if x.strip()]
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        head, cells = _cells(ln)
        cands = None
        if len(cells) < 4 and i < len(lines) and _frag_line(lines[i]):
            wrapped = _wrapped_cells(ln, lines[i:i + 3])
            if wrapped:
                whead, cands, used = wrapped
                if len(cells) >= 3:            # 原样也像完整的一行：也当一个候选
                    cands = [cells] + [c for c in cands if c != cells]
                head, cells = whead, cands[0]
                i += used
        if pending:
            prefix, pcells, skipped = pending
            if not cells:
                cand = prefix + ln
                key = _ALIAS.get(_norm(cand))
                if key:
                    rows.setdefault(key, pcells)
                    pending = None
                    continue
                if _alias_prefix(cand):
                    pending = (cand, pcells, skipped)
                    continue
            if (head and len(cells) >= 3) or skipped >= 4:
                pending = None                 # 下一行数据都来了，半截科目作废，照常处理这一行
            else:
                pending = (prefix, pcells, skipped + 1)    # 页眉、页码：跳过
                continue
        if len(cells) >= 3:
            key = None
            for k in range(0, min(4, len(buffer)) + 1):
                parts = buffer[len(buffer) - k:] if k else []
                key = _ALIAS.get(_norm("".join(parts) + head))
                if key:
                    break
            if key:
                if key not in rows and cands and len(cands) > 1 and alts is not None:
                    alts[key] = cands
                rows.setdefault(key, cells)
            elif _alias_prefix(head):
                pending = (head, cells, 0)
            buffer = []
            continue
        if cells:
            buffer = []
            continue
        buffer = (buffer + [ln])[-4:]
    return rows


def _check(cur: float, prior: float, change: float) -> bool:
    if prior == 0:
        return False
    return abs((cur - prior) / abs(prior) * 100 - change) <= _TOLERANCE


def _amounts(line: str) -> list[float]:
    return [v for v in (_num(t) for t in line.split()) if v is not None]


def _prior_from_statements(all_lines: list[str], ytd: float, change: float,
                           scale: float) -> float | None:
    """在报表里找「年初至今金额 上年同期金额」这一行，用它复算增减。返回上年同期（元）。

    报表的单位可能和主要会计数据表不同（时代新材的表是万元、报表是元），两种都试；
    万元四舍五入到分，换算成元会差几十元，所以按换算后的精度比对。
    表里那一行自己也是「金额 增减」相邻，但拿增减当上年金额复算不可能对得上，不用特意跳过。
    科目名太长时一行会折开（精达 2025 三季报的归母净利润：本期金额、上年同期金额各占一行），
    所以本期金额是这一行最后一个数时，再看下一行的第一个数。
    """
    target = ytd * scale
    for i, ln in enumerate(all_lines):
        vals = _amounts(ln)
        if vals and i + 1 < len(all_lines):
            vals = vals + _amounts(all_lines[i + 1])[:1]
        for j in range(len(vals) - 1):
            for s in {1.0, scale}:
                if abs(vals[j] * s - target) <= 0.006 * scale:
                    prior = vals[j + 1] * s
                    if _check(target, prior, change):
                        return prior
    return None


def _nonrecurring_total(all_lines: list[str]) -> list[float] | None:
    """非经常性损益表的「合计」行：三季报是 [本期, 年初至今]，一季报只有 [本期]。"""
    for ln in all_lines:
        if _norm(ln).startswith("合计"):
            vals = _amounts(ln)
            if vals:
                return vals
    return None


def audit_status(pages_lines: list[list[str]]) -> bool | None:
    """季报封面写「第三季度财务报表是否经审计 □是 √否」；半年报写「本半年度报告未经审计」。

    深交所半年报把这句放在第 30 页之后，读到的页里没有就返回 None（界面写「通常未经审计」）。
    """
    head = " ".join(ln for lines in pages_lines for ln in lines)
    head = re.sub(r"\s+", " ", head)
    if _AUDIT_NO_RE.search(head):
        return False
    if _AUDIT_YES_RE.search(head):
        return True
    return None


def _periodic_item(key: str, cells: list[str], period: str, y0: str, y1: str,
                   scale: float, all_lines: list[str]) -> dict | None:
    nums = [_num(c) for c in cells]
    if period == Q3 and key in FLOW_KEYS:
        if len(cells) != 4 or nums[2] is None or nums[3] is None:
            return None
        ytd, change = nums[2], nums[3]
        item = {"label": LABELS[key], "values": {y0: ytd * scale}, "change": change}
        if key != "np_deducted":                 # 扣非等归母净利润核对完再用合计行核
            prior = _prior_from_statements(all_lines, ytd, change, scale)
            if prior is None:
                return None
            item["values"][y1] = prior
        # 年初至今核对上了，说明这一行四格的列序是对的，第一格就是单季
        if nums[0] is not None and nums[1] is not None:
            item["quarter"] = {"value": nums[0] * scale, "change": nums[1]}
        return item
    if None in nums:
        return None
    if len(nums) == 4:
        nums = [nums[0], nums[2], nums[3]]       # 上年同期拆成调整前/调整后：用调整后
    if len(nums) != 3:
        return None
    cur, prior, change = nums
    if not _check(cur, prior, change):
        return None
    item = {"label": LABELS[key], "values": {y0: cur * scale, y1: prior * scale},
            "change": change}
    if key in BALANCE_KEYS:
        item["basis"] = "本报告期末 vs 上年度末"
    return item


def extract_periodic_from_lines(pages_lines: list[list[str]], period: str,
                                year: int) -> dict | None:
    """季报 / 半年报的主要会计数据。三季报存「年初至今」，单季放在 items[key]["quarter"]。"""
    all_lines = [ln for lines in pages_lines for ln in lines]
    y0, y1 = str(year), str(year - 1)
    for window in _table_windows(pages_lines):
        table = _cut_at_end([ln for _, ln in window])
        window = window[:len(table)]
        alts: dict[str, list[list[str]]] = {}
        rows = parse_periodic_rows(table, alts)
        if "revenue" not in rows or "np_parent" not in rows:
            continue
        unit = detect_unit(table)
        scale = UNIT_SCALE[unit]
        items = {}
        for key, cells in rows.items():
            # 数字折行有几种接法时逐个核对：核对得上的只有一种（或几种结果相同）才算，否则不猜
            got = [it for it in (_periodic_item(key, c, period, y0, y1, scale, all_lines)
                                 for c in alts.get(key, [cells])) if it]
            if got and all(g == got[0] for g in got):
                items[key] = got[0]
        ded, npp = items.get("np_deducted"), items.get("np_parent")
        if period == Q3 and ded:
            total = _nonrecurring_total(all_lines)
            ok = False
            if npp and total and len(total) >= 2:
                diff = npp["values"][y0] - ded["values"][y0]
                ok = any(abs(diff - total[1] * s) <= 0.02 * scale for s in {1.0, scale})
            if not ok:
                items.pop("np_deducted")
        if "revenue" not in items or "np_parent" not in items:
            continue
        return {
            "version": FIN_VERSION,
            "period": period,
            "unit": unit,
            "page": _page_of(window, LABELS["revenue"]),
            "years": [y0, y1],
            "audited": audit_status(pages_lines),
            "items": items,
        }
    return None


def extract(data: bytes, period: str = FY, year: str | int = "") -> dict | None:
    from .pdf_parser import raw_page_lines

    pages_lines = raw_page_lines(data, max_pages=30)
    if period == FY:
        return extract_from_lines(pages_lines)
    if not str(year).isdigit():
        year = detect("\n".join(pages_lines[0]) if pages_lines else "")[0]
    if not str(year).isdigit():
        return None
    return extract_periodic_from_lines(pages_lines, period, int(year))


def load_or_build(doc_id: str) -> dict | None:
    """读 data/<doc_id>/financials.json；没有或版本不对就从 source.pdf 现抽并写回。

    抽不到也写一个 {"version", "items": {}}，免得每次 rerun 都重新读 PDF。
    独立于 INDEX_VERSION：老文档第一次用到时补齐即可，不必重新嵌入。
    """
    folder = config.DATA_DIR / doc_id
    path = folder / FIN_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("version") == FIN_VERSION:
            return cached if cached.get("items") else None
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
        fin = extract(pdf.read_bytes(), period=meta.get("period") or FY,
                      year=meta.get("year", ""))
    except Exception:
        fin = None
    try:
        path.write_text(json.dumps(fin or {"version": FIN_VERSION, "items": {}},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return fin
