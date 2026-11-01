"""结构化财务数据：从年报的「主要会计数据」表里确定性地抽出近三年关键数字。

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
"""
from __future__ import annotations

import json
import re

from . import config

FIN_VERSION = 1
FIN_FILE = "financials.json"

UNIT_SCALE = {"亿元": 1e8, "万元": 1e4, "千元": 1e3, "元": 1.0}

# 科目键 → 年报里的写法（去掉空格和「（元）」之后精确比对）
ITEMS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入",),
    "total_profit": ("利润总额",),
    "np_parent": ("归属于上市公司股东的净利润",),
    "np_deducted": ("归属于上市公司股东的扣除非经常性损益的净利润",),
    "ocf": ("经营活动产生的现金流量净额",),
    "equity_parent": ("归属于上市公司股东的净资产",),
    "total_assets": ("总资产",),
}
LABELS = {k: v[0] for k, v in ITEMS.items()}
_ALIAS = {alias: key for key, aliases in ITEMS.items() for alias in aliases}

_NUM_RE = re.compile(r"-?\d[\d,]*\.\d+%?")
_YEAR_RE = re.compile(r"(20\d\d)\s*年")
_UNIT_DECL_RE = re.compile(r"单位\s*[:：]\s*(亿元|万元|千元|元)")
_UNIT_IN_LABEL_RE = re.compile(r"[（(](亿元|万元|千元|元)[)）]")
# 表的结束：下一张「主要财务指标」表，或下一个一级标题（「八、」）
_END_RE = re.compile(r"^\s*([(（]二[)）]|主要财务指标|[一二三四五六七八九十]{1,3}、)")
_WINDOW = 60          # 从标题往下最多看多少行
_TOLERANCE = 0.1      # 复算增减与披露值允许相差的百分点


def _norm(label: str) -> str:
    return _UNIT_IN_LABEL_RE.sub("", re.sub(r"\s+", "", label))


def _to_float(raw: str) -> float:
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
    """科目可能折成两三行。依次试「本行开头」「上一行 + 本行开头」「上两行 + 本行开头」。"""
    for k in range(0, min(2, len(buffer)) + 1):
        parts = buffer[len(buffer) - k:] if k else []
        key = _ALIAS.get(_norm("".join(parts) + head))
        if key:
            return key
    return None


def parse_rows(lines: list[str]) -> dict[str, list[float]]:
    """逐行扫描，返回 {科目键: [本年, 上年, 增减%, 前年]}（原始单位，未换算）。

    只认**恰好 4 个数**的行：本表的列序固定为本年、上年、增减、前年。
    少一个数（比如「不适用」）就无法确定列的归属，直接跳过，而不是去猜。
    """
    rows: dict[str, list[float]] = {}
    buffer: list[str] = []
    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue
        nums = _NUM_RE.findall(ln)
        if len(nums) == 4:
            head = ln[: ln.find(nums[0])].strip()
            key = _match_label(buffer, head)
            if key and key not in rows:
                rows[key] = [_to_float(n) for n in nums]
            buffer = []
            continue
        if nums or _YEAR_RE.search(ln):
            # 年份表头、或数字个数不对的行：都是边界，前面累积的半截科目作废。
            buffer = []
            continue
        buffer = (buffer + [ln])[-2:]
    return rows


def verify(values: list[float]) -> bool:
    """用本年、上年复算增减，与公司披露的比例对比。对不上 = 列错位或科目抽错。"""
    cur, prior, change, _ = values
    if prior == 0:
        return False
    return abs((cur - prior) / abs(prior) * 100 - change) <= _TOLERANCE


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
            "unit": unit,
            "page": _page_of(window, LABELS["revenue"]),
            "years": [str(year), str(year - 1), str(year - 2)],
            "items": items,
        }
    return None


def extract(data: bytes) -> dict | None:
    from .pdf_parser import raw_page_lines

    return extract_from_lines(raw_page_lines(data, max_pages=30))


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
        fin = extract(pdf.read_bytes())
    except Exception:
        fin = None
    try:
        path.write_text(json.dumps(fin or {"version": FIN_VERSION, "items": {}},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return fin
