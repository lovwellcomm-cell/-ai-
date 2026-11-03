"""三表速读：把合并资产负债表、合并利润表、合并现金流量表读成一张卡。

体检回答「有没有雷」，速读回答「钱从哪来、花到哪去、剩下多少」：只讲结构和同比变化，不打红黄绿灯
（用户 2026-11-03 定）。和 healthcheck.py 一样是确定性抽取，模型不参与；每个数都记着原文页码。

**勾稽自检**是这里的底线：资产 = 负债 + 所有者权益、流入 − 流出 = 净额、三项净额 + 汇率 = 现金净增加、
期初 + 净增加 = 期末、利润总额 − 所得税 = 净利润。哪张表对不上，就不展示那张表的数。

真实报表里踩到的写法（夹具就是这些原文）：
  · 一行是「科目 [附注] 本期 上期」。附注列有「(五)1」「七、1」「五、(一)」几种，要剥掉
  · 只有一列有数的行（精达「交易性金融资产 2,575,806.90」）光看文字分不清是哪一列：
    用 PDF 里这个数的横坐标，和同页两列都有数的行比（_Locator）
  · 万科负数写成括号「(988,124,860.96)」；空值写「-」
  · 科目折行：万科「处置固定资产、无形资产和其他长期资产收回的现金 / 净额 3,633,…」（数在第二行），
    也有数在第一行、「益）合计」落到第二行的；精达三季报「盈余公积 … 一般风险准备」把下一行的科目粘在行尾
  · 单位：「单位：元 币种：人民币」，万科单独一行「人民币元」
  · 季报的利润表、现金流量表是「年初至报告期末 vs 上年同期」，资产负债表是「期末 vs 上年末」
"""
from __future__ import annotations

import json
import re

from . import config
from .financials import UNIT_SCALE
from .pdf_parser import PAGE_NUM_RE
from .periods import FY, H1, Q1, Q3

STMT_VERSION = 1
STMT_FILE = "statements.json"

BS, IS, CF = "bs", "is", "cf"
NAMES = {BS: "资产负债表", IS: "利润表", CF: "现金流量表"}

# 报表标题独占一行：「合并资产负债表」「1、合并利润表」「（三）合并现金流量表」「合并资产负债表（续）」，
# 深交所季报写「2、合并年初到报告期末利润表」。目录里的「合并资产负债表 7 - 8」不算
_TITLE_RE = re.compile(r"^(?:\d+[、.．]|[一二三四五六七八九十]+、|[(（][一二三四五六七八九十\d]+[)）])?"
                       r"(合并|母公司)(?:年初到报告期末)?(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)"
                       r"(?:[(（]续[)）])?$")
_KIND_OF = {"资产负债表": BS, "利润表": IS, "现金流量表": CF}
# 报表末尾的签章行
_END_RE = re.compile(r"^(?:公司)?(?:负责人|法定代表人)")
_MAX_PAGES = 5
# 页眉：「立讯精密工业股份有限公司 2025 年年度报告全文」「… 2025 年第三季度报告」
_REPORT_HEAD_RE = re.compile(r"(?:年度|季度)报告")

# 两位小数的金额，括号或负号为负；左右不能挨着数字
_AMT_RE = re.compile(r"(?<![\d.,])[(（]?[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}[)）]?(?![\d%])")
_GLUE_RE = re.compile(r"(\.\d{2})(?=\d)")
_UNIT_RE = re.compile(r"单位\s*[:：]\s*(?:人民币)?\s*(亿元|万元|千元|元)|^\s*人民币\s*(亿元|万元|千元|元)\s*$")
_CJK_RE = re.compile(r"[一-鿿]")

# 科目键 → 写法（去空白、去括号里的说明、去序号和附注之后精确比对）
ITEMS: dict[str, dict[str, tuple[str, ...]]] = {
    BS: {
        "cash": ("货币资金",),
        "trading": ("交易性金融资产",),
        "notes_rec": ("应收票据",),
        "ar": ("应收账款",),
        "ar_fin": ("应收款项融资",),
        "prepay": ("预付款项", "预付账款"),
        "inventory": ("存货",),
        "contract_assets": ("合同资产",),
        "cur_assets": ("流动资产合计",),
        "total_assets": ("资产总计", "资产合计"),
        "st_loan": ("短期借款",),
        "notes_pay": ("应付票据",),
        "ap": ("应付账款",),
        "adv": ("预收款项", "预收账款"),
        "contract_liab": ("合同负债",),
        "due_1y": ("一年内到期的非流动负债",),
        "cur_liab": ("流动负债合计",),
        "lt_loan": ("长期借款",),
        "bonds": ("应付债券",),
        "lease": ("租赁负债",),
        "total_liab": ("负债合计",),
        # 「归属于母公司所有者权益（或股东权 / 益）合计」后半截常被分页隔开（茅台 2025、精达 2025 三季报）：
        # 没有「合计」的写法在资产负债表里只会是这一行
        "equity_parent": ("归属于母公司所有者权益合计", "归属于母公司股东权益合计",
                          "归属于母公司所有者权益", "归属于母公司股东权益"),
        "equity": ("所有者权益合计", "股东权益合计"),
        "liab_equity": ("负债和所有者权益总计", "负债和股东权益总计", "负债及所有者权益总计",
                        "负债及股东权益总计", "负债和所有者权益合计", "负债和股东权益合计"),
    },
    IS: {
        "total_rev": ("营业总收入",),
        "revenue": ("营业收入",),
        "total_cost": ("营业总成本",),
        "cost": ("营业成本",),
        "tax": ("税金及附加",),
        "selling": ("销售费用",),
        "admin": ("管理费用",),
        "rd": ("研发费用",),
        "fin_exp": ("财务费用",),
        "credit_imp": ("信用减值损失",),
        "asset_imp": ("资产减值损失",),
        "op_profit": ("营业利润",),
        "non_op_in": ("营业外收入",),
        "non_op_out": ("营业外支出",),
        "total_profit": ("利润总额",),
        "income_tax": ("所得税费用", "所得税"),
        "net_profit": ("净利润",),
        "np_parent": ("归属于母公司股东的净利润", "归属于母公司所有者的净利润",
                      "归属于母公司普通股股东的净利润"),
        "minority": ("少数股东损益",),
        # 只用来认金融类公司（银行、证券、保险的利润表从这几行开始）
        "interest_net": ("利息净收入",),
        "premium": ("已赚保费",),
    },
    CF: {
        "sales_cash": ("销售商品、提供劳务收到的现金",),
        "op_in": ("经营活动现金流入小计",),
        "op_out": ("经营活动现金流出小计",),
        "op_net": ("经营活动产生的现金流量净额",),
        "capex": ("购建固定资产、无形资产和其他长期资产支付的现金",),
        "inv_in": ("投资活动现金流入小计",),
        "inv_out": ("投资活动现金流出小计",),
        "inv_net": ("投资活动产生的现金流量净额",),
        "dividend": ("分配股利、利润或偿付利息支付的现金",),
        "fin_in": ("筹资活动现金流入小计",),
        "fin_out": ("筹资活动现金流出小计",),
        "fin_net": ("筹资活动产生的现金流量净额",),
        "fx": ("汇率变动对现金及现金等价物的影响",),
        "net_inc": ("现金及现金等价物净增加额",),
        "begin": ("期初现金及现金等价物余额",),
        "end": ("期末现金及现金等价物余额",),
    },
}
_ALIAS = {kind: {a: k for k, al in items.items() for a in al} for kind, items in ITEMS.items()}


# ---------------------------------------------------------------- 一行 → 科目和两列数

def norm_label(text: str) -> str:
    """「四、净利润（净亏损以“－”号填列）」→「净利润」；「货币资金 七、1」→「货币资金」。"""
    s = re.sub(r"\s+", "", text)
    prev = None
    while prev != s:                                       # 括号里的说明（可能嵌套一层）
        prev = s
        s = re.sub(r"[(（][^()（）]*[)）]", "", s)
    s = re.sub(r"[(（].*$", "", s)                         # 没闭合的说明（折到下一行了）
    s = re.sub(r"^(?:[一二三四五六七八九十]+[、.．]|\d+[、.．]|加[:：]|减[:：]|其中[:：])+", "", s)
    # 行尾的附注编号：「七、1」「五、(一)」去掉括号后剩下的「五、」「(五)1」剩下的「1」
    s = re.sub(r"(?:附注)?(?:[一二三四五六七八九十]+[、.．]?)?[\d、.．]*$", "", s)
    # 现金流净额的几种说法：安克「投资活动产生/(使用)的」「筹资活动使用的」，也有写「所用的」
    s = re.sub(r"活动(?:产生/?|使用|所用)的现金流量净额$", "活动产生的现金流量净额", s)
    # 年报里有的写「年初 / 年末现金及现金等价物余额」
    s = re.sub(r"^年([初末])现金及现金等价物余额$", lambda m: ("期初" if m.group(1) == "初" else "期末")
               + "现金及现金等价物余额", s)
    return s.rstrip("：:")


def _segments(line: str) -> list[str]:
    """PDF 偶尔把几行粘成一行（安克「发行债券收到的现金 1,096,920,000.00 - 收到其他与筹资活动有关的现金 … - -
    筹资活动现金流入小计 2,337,…」，精达三季报「盈余公积 … 一般风险准备」）：数（或表示空值的「-」）后面
    又出现汉字的地方就是下一行。"""
    text = _GLUE_RE.sub(r"\1 ", line)
    cuts, seen_amount = [0], False
    for m in re.finditer(rf"{_AMT_RE.pattern}|(?<!\S)[-—–－]{{1,2}}(?!\S)|[一-鿿]+", text):
        if _CJK_RE.match(m.group(0)):
            if seen_amount:
                cuts.append(m.start())
                seen_amount = False
        else:
            seen_amount = True
    return [text[a:b].strip() for a, b in zip(cuts, cuts[1:] + [len(text)]) if text[a:b].strip()]


def _tokens(segment: str) -> tuple[str, list[tuple[float, str]]]:
    """一段 → (科目文字, [(金额, 原文数字)])。原文数字不带括号和负号，用来在 PDF 里找位置。"""
    found = list(_AMT_RE.finditer(segment))
    amounts = []
    for m in found:
        raw = m.group(0)
        digits = re.sub(r"[^\d.,]", "", raw)
        v = float(digits.replace(",", ""))
        amounts.append((-v if raw[0] in "-−(（" else v, digits))
    return (segment[:found[0].start()] if found else segment).strip(), amounts


class _Locator:
    """单列有数的行判断是哪一列：在 PDF 那一页里找这个数的右边缘横坐标。

    pdf 为 None 时（夹具只有文字）一律判断不了，这种行不取数。"""

    def __init__(self, pdf=None):
        self.pdf = pdf
        self._pages: dict[int, object] = {}

    def right_edges(self, page: int, digits: str) -> list[float]:
        if self.pdf is None:
            return []
        tp = self._pages.get(page)
        if tp is None:
            tp = self._pages[page] = self.pdf[page - 1].get_textpage()
        out = []
        searcher = tp.search(digits)
        try:
            while (hit := searcher.get_next()):
                i, n = hit
                out.append(tp.get_charbox(i + n - 1)[2])
        finally:
            searcher.close()
        return out

    def close(self):
        for tp in self._pages.values():
            tp.close()
        self._pages.clear()


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2]


def parse_rows(rows: list[tuple[int, str]], kind: str, locator: _Locator | None = None) -> dict[str, dict]:
    """[(页码, 一行)] → {科目键: {"cur", "prev", "page"}}（报表上的原值，未乘单位）。同一科目只取第一次出现。"""
    alias = _ALIAS[kind]
    locator = locator or _Locator()
    parsed = []          # (页码, 科目键, [(金额, 原文)])
    pending: list[str] = []   # 前面几行没有数的文字：可能是折成两三行的科目（巨石「所有者权益（或股东权 / 益）合计 / 数」）
    dangling = None      # 上一行有数但科目没认出来：可能科目的后半截在下一行
    last_single = None   # 上一行认出的科目只带了一个数
    segs = [(page, seg) for page, line in rows for seg in _segments(line)]
    for page, seg in segs:
        label, amounts = _tokens(seg)
        # 两列的数各占一行（精达 2025 三季报「归属于母公司股东的净利润」）：只有数、没有字，接到上一项后面
        if amounts and not label and last_single is not None and last_single is parsed[-1]:
            parsed[-1] = (parsed[-1][0], parsed[-1][1], parsed[-1][2] + amounts[:1])
            last_single = None
            continue
        last_single = None
        if not amounts:
            if dangling is not None:
                key = alias.get(norm_label(dangling[1] + label))
                if key:
                    parsed.append((dangling[0], key, dangling[2]))
                    dangling = None
                    pending = []
                    continue
            dangling = None
            pending = (pending + [label])[-3:]
            continue
        key = alias.get(norm_label(label))
        # 前面粘着几个空着的科目（七匹狼三季报整页成一行：「… 衍生金融负债 应付票据 248,627,…」）：数归最后那个
        words = label.split()
        for n in range(1, min(len(words), 4)):
            if key:
                break
            key = alias.get(norm_label(" ".join(words[-n:])))
        # 科目没认出来：从最近一行起往前接，最多接三行
        for n in range(1, len(pending) + 1):
            if key:
                break
            key = alias.get(norm_label("".join(pending[-n:]) + label))
        dangling = None if key else (page, label, amounts)
        if key:
            parsed.append((page, key, amounts))
            if len(amounts) == 1:
                last_single = parsed[-1]
        pending = []

    # 每页两列的右边缘：取两列都有数的行
    cols: dict[int, list[list[float]]] = {}
    for page, _, amounts in parsed:
        if len(amounts) == 2 and len(cols.setdefault(page, [[], []])[0]) < 6:
            a = locator.right_edges(page, amounts[0][1])
            b = locator.right_edges(page, amounts[1][1])
            if len(a) == 1 and len(b) == 1 and a[0] < b[0]:
                cols[page][0].append(a[0])
                cols[page][1].append(b[0])
    edges = {p: (_median(c[0]), _median(c[1])) for p, c in cols.items() if c[0]}

    out: dict[str, dict] = {}
    for page, key, amounts in parsed:
        if key in out:
            continue
        if len(amounts) >= 2:
            out[key] = {"cur": amounts[0][0], "prev": amounts[1][0], "page": page}
            continue
        v, digits = amounts[0]
        edge = edges.get(page) or next((edges[p] for p in sorted(edges, key=lambda p: abs(p - page))), None)
        xs = locator.right_edges(page, digits) if edge else []
        sides = {0 if abs(x - edge[0]) < abs(x - edge[1]) else 1 for x in xs}
        if len(sides) == 1:
            side = sides.pop()
            out[key] = {"cur": v if side == 0 else None, "prev": v if side == 1 else None, "page": page}
    return out


# ---------------------------------------------------------------- 找报表

def find_statements(pages_lines: list[list[str]]) -> dict[str, dict]:
    """{kind: {"rows": [(页码, 行)], "unit": "元", "page": 标题所在页}}：每种报表第一次出现的合并报表。"""
    found: dict[str, dict] = {}
    for i, lines in enumerate(pages_lines):
        for li, ln in enumerate(lines):
            m = _TITLE_RE.match(re.sub(r"\s+", "", ln))
            if not m or m.group(1) != "合并":
                continue
            kind = _KIND_OF.get(m.group(2))
            if kind is None or kind in found:
                continue
            found[kind] = {"rows": _rows_from(pages_lines, i, li), "page": i + 1,
                           "unit": _unit_near(pages_lines[i], li)}
    return found


def _rows_from(pages_lines, page_i: int, line_i: int) -> list[tuple[int, str]]:
    """标题之后到签章行（或下一张表的标题）为止的行。页眉、页码、续页重复的表头去掉 ——
    折行的科目常常被分页隔开（立讯「购建固定资产、无形资产和其他长 / 页眉 / 页码 / 期资产支付的现金」）。"""
    rows = []
    for i in range(page_i, min(page_i + _MAX_PAGES, len(pages_lines))):
        start = line_i + 1 if i == page_i else 0
        for li, ln in enumerate(pages_lines[i][start:], start):
            s = re.sub(r"\s+", "", ln)
            if _END_RE.match(s):
                return rows
            m = _TITLE_RE.match(s)
            if m and not (m.group(1) == "合并" and "续" in s):
                return rows
            if (not s or PAGE_NUM_RE.match(ln) or s.startswith("项目")
                    or (li < 3 and _REPORT_HEAD_RE.search(s) and not _AMT_RE.search(ln))):
                continue
            rows.append((i + 1, ln))
    return rows


def _unit_near(lines: list[str], title_i: int) -> str:
    """标题后面 12 行内的「单位：元」「人民币元」；没有就往前找同一页的；都没有按元。"""
    for ln in lines[title_i + 1:title_i + 13] + lines[:title_i][::-1]:
        m = _UNIT_RE.search(ln)
        if m:
            return m.group(1) or m.group(2)
    return "元"


# ---------------------------------------------------------------- 勾稽自检

def _close(a: float, b: float) -> bool:
    """报表上的原值（按报表自己的单位，两位小数）：允许几个 0.01 的尾差，大数再留一点相对误差。"""
    return abs(a - b) <= 0.05 + 1e-9 * max(abs(a), abs(b))


# (说明, 左边各项（键, 符号）, 右边那一项, 是否必须有)
_CHECKS = {
    BS: [
        ("资产总计 = 负债合计 + 所有者权益合计", (("total_liab", 1), ("equity", 1)), "total_assets", True),
        ("资产总计 = 负债和所有者权益总计", (("liab_equity", 1),), "total_assets", False),
    ],
    IS: [
        ("利润总额 − 所得税 = 净利润", (("total_profit", 1), ("income_tax", -1)), "net_profit", True),
        ("营业利润 + 营业外收入 − 营业外支出 = 利润总额",
         (("op_profit", 1), ("non_op_in", 1), ("non_op_out", -1)), "total_profit", False),
        ("归母净利润 + 少数股东损益 = 净利润", (("np_parent", 1), ("minority", 1)), "net_profit", False),
    ],
    CF: [
        ("经营活动：流入 − 流出 = 净额", (("op_in", 1), ("op_out", -1)), "op_net", False),
        ("投资活动：流入 − 流出 = 净额", (("inv_in", 1), ("inv_out", -1)), "inv_net", False),
        ("筹资活动：流入 − 流出 = 净额", (("fin_in", 1), ("fin_out", -1)), "fin_net", False),
        ("经营 + 投资 + 筹资 + 汇率影响 = 现金净增加额",
         (("op_net", 1), ("inv_net", 1), ("fin_net", 1), ("fx", 1)), "net_inc", True),
        ("期初现金 + 净增加额 = 期末现金", (("begin", 1), ("net_inc", 1)), "end", False),
    ],
}
# 可以没有这一行（空着就是 0）的科目
_OPTIONAL_ZERO = {"non_op_in", "non_op_out", "fx", "minority"}
_REQUIRED = {BS: ("total_assets", "total_liab", "equity"), IS: ("total_profit", "income_tax", "net_profit"),
             CF: ("op_net", "inv_net", "fin_net", "net_inc")}


def _val(items: dict[str, dict], key: str, col: str) -> float | None:
    """某科目某一列的数。行在、这一列空着 = 0（单列有数的行，另一列就是空的）；
    行不在：可以空着的科目按 0，其余返回 None（不知道）。"""
    if key not in items:
        return 0.0 if key in _OPTIONAL_ZERO else None
    v = items[key].get(col)
    return 0.0 if v is None else v


def run_checks(kind: str, items: dict[str, dict]) -> tuple[bool, list[dict]]:
    """→ (这张表能不能用, [{"desc", "ok"}])。必须的那条两列都要对上；可选的那几条能算就算，算了就必须对。"""
    results = []
    for col in ("cur", "prev"):
        for desc, terms, total, required in _CHECKS[kind]:
            vals = [None if v is None else sign * v
                    for v, sign in ((_val(items, key, col), sign) for key, sign in terms)]
            right = _val(items, total, col)
            if right is None or None in vals:
                if required:
                    return False, results + [{"desc": desc, "ok": False, "col": col}]
                continue
            results.append({"desc": desc, "ok": _close(sum(vals), right), "col": col})
    ok = all(r["ok"] for r in results)
    # 给人看的只列一遍说明（两列都核过）
    seen, shown = set(), []
    for r in results:
        if r["desc"] not in seen:
            seen.add(r["desc"])
            shown.append({"desc": r["desc"], "ok": all(x["ok"] for x in results if x["desc"] == r["desc"])})
    return ok, shown


# ---------------------------------------------------------------- 汇总

def build_from_lines(pages_lines: list[list[str]], period: str = FY, locator: _Locator | None = None) -> dict:
    found = find_statements(pages_lines)
    out: dict = {"version": STMT_VERSION, "period": period}
    for kind in (BS, IS, CF):
        st = found.get(kind)
        if not st:
            out[kind] = {"ok": False, "why": "没找到这张表"}
            continue
        unit = st["unit"]
        scale = UNIT_SCALE[unit]
        raw = parse_rows(st["rows"], kind, locator)
        if kind == IS and ("interest_net" in raw or "premium" in raw) and "cost" not in raw:
            out[kind] = {"ok": False, "why": "金融类公司的利润表结构不同，暂不支持", "financial": True}
            continue
        ok, checks = run_checks(kind, raw)
        items = {k: {"cur": None if v["cur"] is None else v["cur"] * scale,
                     "prev": None if v["prev"] is None else v["prev"] * scale,
                     "page": v["page"]} for k, v in raw.items()}
        missing = [k for k in _REQUIRED[kind] if k not in raw]
        why = "" if ok else ("缺少" + "、".join(ITEMS[kind][k][0] for k in missing) if missing else "勾稽关系没对上")
        out[kind] = {"ok": ok, "page": st["page"], "unit": unit, "items": items, "checks": checks, "why": why}
    return out


def build(data: bytes, period: str = FY) -> dict:
    from .pdf_parser import _page_lines, open_pdf

    pdf = open_pdf(data)
    locator = _Locator(pdf)
    try:
        pages_lines = [_page_lines(pdf, i) for i in range(len(pdf))]
        return build_from_lines(pages_lines, period, locator)
    finally:
        locator.close()
        pdf.close()


def load_or_build(doc_id: str) -> dict | None:
    """读 data/<doc_id>/statements.json；没有或版本不对就从 source.pdf 现做并写回。"""
    folder = config.DATA_DIR / doc_id
    path = folder / STMT_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("version") == STMT_VERSION:
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
        st = build(pdf.read_bytes(), period=meta.get("period") or FY)
    except Exception:
        return None
    try:
        path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return st


# ---------------------------------------------------------------- 卡片与正文
#
# 速读回合不检索、不调模型：卡片和正文都是程序按上面的数写的。只讲结构和变化，不下「好 / 坏」的结论。

# 两列各叫什么：资产负债表是两个时点，利润表和现金流量表是两段时间（季报是年初至报告期末）
_BS_COLS = {FY: ("{y} 年末", "{p} 年末"), Q1: ("{y} 年 3 月末", "{p} 年末"),
            H1: ("{y} 年 6 月末", "{p} 年末"), Q3: ("{y} 年 9 月末", "{p} 年末")}
_FLOW_COLS = {FY: ("{y} 年", "{p} 年"), Q1: ("{y} 年一季度", "{p} 年一季度"),
              H1: ("{y} 年上半年", "{p} 年上半年"), Q3: ("{y} 年前三季度", "{p} 年前三季度")}

# 现金流三项净额的正负 → 一句话（经营, 投资, 筹资；≥ 0 记「+」）
PORTRAITS = {
    "+--": "主业赚来的钱，一部分投了出去，一部分用来还债或分红",
    "+-+": "主业在赚钱，同时还在借钱或融资，一起投向扩张",
    "++-": "主业在赚钱，又收回了一些投资，钱主要用来还债或分红",
    "+++": "三路都是净流入，钱在往账上攒",
    "--+": "主业还在花钱，投资和日常开销靠融资撑着",
    "-++": "主业在花钱，靠收回投资和融资补上",
    "-+-": "主业在花钱，还要还债或分红，靠收回投资来补",
    "---": "三路都是净流出，靠账上原有的现金",
}

DEBT_KEYS = ("st_loan", "due_1y", "lt_loan", "bonds", "lease")
DEBT_NOTE = "短期借款 + 一年内到期的非流动负债 + 长期借款 + 应付债券 + 租赁负债"
REC_KEYS = ("notes_rec", "ar", "ar_fin")
REC_NOTE = "应收票据 + 应收账款 + 应收款项融资"
PAY_KEYS = ("notes_pay", "ap", "adv", "contract_liab")
PAY_NOTE = "应付票据 + 应付账款 + 预收款项 + 合同负债"
FEE_KEYS = ("selling", "admin", "rd", "fin_exp")


def col_labels(period: str, year) -> tuple[tuple[str, str], tuple[str, str]]:
    """(资产负债表两列, 利润表和现金流量表两列)。年份不知道时写「期末 / 上年末」「本期 / 上年同期」。"""
    y = str(year or "")
    if not y.isdigit():
        return ("期末", "上年末"), ("本期", "上年同期")
    p = str(int(y) - 1)
    bs, fl = _BS_COLS.get(period, _BS_COLS[FY]), _FLOW_COLS.get(period, _FLOW_COLS[FY])
    return (bs[0].format(y=y, p=p), bs[1].format(y=y, p=p)), (fl[0].format(y=y, p=p), fl[1].format(y=y, p=p))


class _Table:
    """一张已核对过的报表：按列取数。行在、这一列空着 = 0；行不在 = None。"""

    def __init__(self, st: dict):
        self.items = st.get("items") or {}
        self.page = st.get("page")

    def get(self, key: str, col: str = "cur") -> float | None:
        it = self.items.get(key)
        if it is None:
            return None
        return it.get(col) or 0.0

    def total(self, keys, col: str = "cur") -> float:
        return sum(self.get(k, col) or 0.0 for k in keys)

    def page_of(self, *keys) -> int | None:
        for k in keys:
            if k in self.items:
                return self.items[k].get("page")
        return self.page


def _scale(v: float) -> tuple[float, str]:
    return (1e8, "亿元") if abs(v) >= 1e8 else (1e4, "万元")


def _amt(v: float | None, scale: float) -> str:
    return "—" if v is None else f"{v / scale:,.2f}"


def _money(v: float) -> str:
    """正文里的金额：「12.34 亿元」「5,678.90 万元」"""
    s, u = _scale(v)
    return f"{v / s:,.2f} {u}"


def _chg(cur: float | None, prev: float | None, scale: float) -> str:
    """变化：上期为正就写百分比；上期为零或负数时百分比没有意义，写增减额。"""
    if cur is None or prev is None:
        return ""
    if prev > 0 and cur >= 0:
        return f"{(cur / prev - 1) * 100:+.1f}%"
    if cur == prev:
        return "持平"
    return f"{(cur - prev) / scale:+,.2f}"


def _pp(cur: float | None, prev: float | None) -> str:
    return "" if cur is None or prev is None else f"{cur - prev:+.1f} 个百分点"


def _ratio(num: float | None, den: float | None) -> float | None:
    return None if num is None or not den else num / den * 100


def _row(label: str, cur, prev, scale: float, page, hint: str = "", ratio: bool = False) -> dict:
    if ratio:
        return {"label": label, "cur": "—" if cur is None else f"{cur:.1f}%",
                "prev": "—" if prev is None else f"{prev:.1f}%", "chg": _pp(cur, prev), "page": page, "hint": hint}
    return {"label": label, "cur": _amt(cur, scale), "prev": _amt(prev, scale),
            "chg": _chg(cur, prev, scale), "page": page, "hint": hint}


def _bs_section(t: _Table, cols, scale: float) -> dict:
    ta, tl = t.get("total_assets"), t.get("total_liab")
    ta0, tl0 = t.get("total_assets", "prev"), t.get("total_liab", "prev")
    r, r0 = _ratio(tl, ta), _ratio(tl0, ta0)
    debt, debt0 = t.total(DEBT_KEYS), t.total(DEBT_KEYS, "prev")
    cash, cash0 = t.get("cash"), t.get("cash", "prev")
    rec, rec0 = t.total(REC_KEYS), t.total(REC_KEYS, "prev")
    pay, pay0 = t.total(PAY_KEYS), t.total(PAY_KEYS, "prev")
    rows = [
        _row("总资产", ta, ta0, scale, t.page_of("total_assets")),
        _row("负债合计", tl, tl0, scale, t.page_of("total_liab")),
        _row("资产负债率", r, r0, scale, t.page_of("total_liab"), "负债合计 ÷ 总资产", ratio=True),
        _row("货币资金", cash, cash0, scale, t.page_of("cash")),
        _row("有息负债", debt, debt0, scale, t.page_of(*DEBT_KEYS), DEBT_NOTE),
        _row("应收", rec, rec0, scale, t.page_of(*REC_KEYS), REC_NOTE),
        _row("存货", t.get("inventory"), t.get("inventory", "prev"), scale, t.page_of("inventory")),
        _row("应付和预收", pay, pay0, scale, t.page_of(*PAY_KEYS), PAY_NOTE),
        _row("归母净资产", t.get("equity_parent"), t.get("equity_parent", "prev"), scale,
             t.page_of("equity_parent"), "归属于母公司所有者权益合计"),
    ]
    lead = [f"总资产 {_money(ta)}，负债 {_money(tl)}，资产负债率 {r:.1f}%"
            + (f"（{cols[1]} {r0:.1f}%）" if r0 is not None else "") + "。"]
    if cash is not None:
        if debt == 0:
            lead.append(f"账上货币资金 {_money(cash)}，没有借款、债券这类要付利息的负债。")
        elif cash >= debt:
            lead.append(f"账上货币资金 {_money(cash)}，有息负债 {_money(debt)}，现金比借款多 {_money(cash - debt)}。")
        else:
            lead.append(f"有息负债 {_money(debt)}，账上货币资金 {_money(cash)}，借款比现金多 {_money(debt - cash)}。")
    if rec or pay:
        if pay > rec:
            lead.append(f"它欠上下游的（应付和预收 {_money(pay)}）比上下游欠它的（应收 {_money(rec)}）多，"
                        "是在用供应商和客户的钱周转。")
        else:
            lead.append(f"上下游欠它的（应收 {_money(rec)}）比它欠上下游的（应付和预收 {_money(pay)}）多，"
                        "有一部分资金压在客户那里。")
    return {"key": BS, "title": "资产负债表 · 家底", "cols": list(cols), "page": t.page,
            "lead": "".join(lead), "rows": rows}


def per_hundred(t: _Table, col: str = "cur") -> list[dict] | None:
    """每 100 元营业收入去了哪：成本、税金、四项费用、减值、所得税，剩下的就是净利润。
    投资收益、其他收益、营业外收支这些合在「其他收支」里倒算，保证几项加起来正好是 100 → 净利润。"""
    rev = t.get("revenue", col) or t.get("total_rev", col)
    np_ = t.get("net_profit", col)
    if not rev or rev <= 0 or np_ is None:
        return None
    imp = -((t.get("credit_imp", col) or 0.0) + (t.get("asset_imp", col) or 0.0))
    parts = [("营业成本", t.get("cost", col) or 0.0, "cost"), ("税金及附加", t.get("tax", col) or 0.0, "tax"),
             ("销售费用", t.get("selling", col) or 0.0, "selling"), ("管理费用", t.get("admin", col) or 0.0, "admin"),
             ("研发费用", t.get("rd", col) or 0.0, "rd"), ("财务费用", t.get("fin_exp", col) or 0.0, "fin_exp"),
             ("减值损失", imp, "credit_imp"), ("所得税", t.get("income_tax", col) or 0.0, "income_tax")]
    other = np_ - (rev - sum(v for _, v, _ in parts))
    out = [{"label": lb, "v": round(v / rev * 100, 1), "page": t.page_of(k)} for lb, v, k in parts if v]
    if abs(other / rev * 100) >= 0.05:
        out.append({"label": "其他收支", "v": round(-other / rev * 100, 1), "page": t.page_of("op_profit"),
                    "hint": "投资收益、其他收益、营业外收支等，按净利润倒算；负数表示是净收入"})
    out.append({"label": "净利润", "v": round(np_ / rev * 100, 1), "page": t.page_of("net_profit"), "net": True})
    return out


def _left(v: float) -> str:
    """每 100 元收入最后剩下的：「剩下净利润 2.5 元」「亏损 39.3 元」"""
    return f"剩下净利润 {v:.1f} 元" if v >= 0 else f"亏损 {-v:.1f} 元"


def _is_section(t: _Table, cols, scale: float) -> dict:
    rev, rev0 = (t.get("revenue") or t.get("total_rev")), (t.get("revenue", "prev") or t.get("total_rev", "prev"))
    cost, cost0 = t.get("cost"), t.get("cost", "prev")
    gm = _ratio(rev - cost, rev) if rev and cost is not None else None
    gm0 = _ratio(rev0 - cost0, rev0) if rev0 and cost0 is not None else None
    np_, np0 = t.get("net_profit"), t.get("net_profit", "prev")
    rows = [
        _row("营业收入", rev, rev0, scale, t.page_of("revenue", "total_rev")),
        _row("毛利率", gm, gm0, scale, t.page_of("cost"), "(营业收入 − 营业成本) ÷ 营业收入", ratio=True),
        _row("期间费用率", _ratio(t.total(FEE_KEYS), rev), _ratio(t.total(FEE_KEYS, "prev"), rev0), scale,
             t.page_of(*FEE_KEYS), "销售、管理、研发、财务四项费用 ÷ 营业收入", ratio=True),
        _row("净利润", np_, np0, scale, t.page_of("net_profit"), "含少数股东损益"),
        _row("归母净利润", t.get("np_parent"), t.get("np_parent", "prev"), scale, t.page_of("np_parent"),
             "归属于母公司股东的净利润"),
        _row("净利率", _ratio(np_, rev), _ratio(np0, rev0), scale, t.page_of("net_profit"), "净利润 ÷ 营业收入",
             ratio=True),
    ]
    flow = per_hundred(t)
    lead = []
    if rev is not None:
        c = _chg(rev, rev0, scale)
        grow = f"，比 {cols[1]}{'增长' if c.startswith('+') else '下降'} {c.lstrip('+-')}" if c.endswith("%") else ""
        lead.append(f"营业收入 {_money(rev)}{grow}。")
    if flow:
        by = {x["label"]: x["v"] for x in flow}
        fees = round(sum(by.get(k, 0) for k in ("销售费用", "管理费用", "研发费用", "财务费用")), 1)
        bits = [f"营业成本 {by.get('营业成本', 0):.1f} 元", f"四项费用 {fees:.1f} 元"]
        if by.get("减值损失"):
            bits.append(f"减值损失 {by['减值损失']:.1f} 元")
        if by.get("所得税", 0) > 0:
            bits.append(f"所得税 {by['所得税']:.1f} 元")
        # 投资收益、补助这些合起来超过 1 元就点明：七匹狼 2025 成本费用加起来已经超过 100 元，
        # 净利润靠投资收益补回 14.1 元，不说的话读者对不上账
        other = by.get("其他收支", 0)
        if other <= -1:
            bits.append(f"投资收益、补助等其他收支补回 {-other:.1f} 元")
        elif other >= 1:
            bits.append(f"其他收支净支出 {other:.1f} 元")
        prev_flow = per_hundred(t, "prev")
        v0 = prev_flow[-1]["v"] if prev_flow else None
        was = "" if v0 is None else f"（{cols[1]}{'为' if v0 >= 0 else '亏损'} {abs(v0):.1f} 元）"
        lead.append("每 100 元收入里，" + "、".join(bits) + f"，最后{_left(by['净利润'])}{was}。")
    if gm is not None and gm0 is not None:
        lead.append(f"毛利率 {gm:.1f}%（{cols[1]} {gm0:.1f}%）。")
    return {"key": IS, "title": "利润表 · 每 100 元收入去了哪", "cols": list(cols), "page": t.page,
            "lead": "".join(lead), "rows": rows, "flow": flow or []}


def _flow_word(v: float) -> str:
    return "净流入" if v >= 0 else "净流出"


def _cf_section(t: _Table, cols, scale: float, revenue: float | None) -> dict:
    op, inv, fin = t.get("op_net"), t.get("inv_net"), t.get("fin_net")
    op0, capex, capex0 = t.get("op_net", "prev"), t.get("capex"), t.get("capex", "prev")
    fcf = None if capex is None else op - capex
    fcf0 = None if capex0 is None or op0 is None else op0 - capex0
    rows = [
        _row("经营活动净额", op, op0, scale, t.page_of("op_net")),
        _row("投资活动净额", inv, t.get("inv_net", "prev"), scale, t.page_of("inv_net")),
        _row("筹资活动净额", fin, t.get("fin_net", "prev"), scale, t.page_of("fin_net")),
        _row("资本开支", capex, capex0, scale, t.page_of("capex"), "购建固定资产、无形资产和其他长期资产支付的现金"),
        _row("自由现金流", fcf, fcf0, scale, t.page_of("capex"), "经营活动净额 − 资本开支"),
        _row("分红和付息", t.get("dividend"), t.get("dividend", "prev"), scale, t.page_of("dividend"),
             "分配股利、利润或偿付利息支付的现金"),
        _row("期末现金", t.get("end"), t.get("end", "prev"), scale, t.page_of("end"), "期末现金及现金等价物余额"),
    ]
    sign = "".join("+" if v >= 0 else "-" for v in (op, inv, fin))
    lead = [f"经营活动{_flow_word(op)} {_money(abs(op))}，投资活动{_flow_word(inv)} {_money(abs(inv))}，"
            f"筹资活动{_flow_word(fin)} {_money(abs(fin))}：{PORTRAITS[sign]}。"]
    cash_ratio = _ratio(t.get("sales_cash"), revenue)
    if cash_ratio is not None:
        lead.append(f"卖货收到的现金相当于营业收入的 {cash_ratio:.0f}%（收到的现金含增值税，所以常略高于 100%）。")
    if fcf is not None:
        lead.append(f"经营活动净额减去资本开支，自由现金流 {_money(fcf)}。")
    return {"key": CF, "title": "现金流量表 · 钱的来去", "cols": list(cols), "page": t.page,
            "lead": "".join(lead), "rows": rows, "portrait": PORTRAITS[sign]}


def card(st: dict, period: str = FY, year="") -> dict:
    """速读卡：三块（对不上勾稽的那张表不出），每块一段导语 + 一张小表，每行带页码。
    金额统一用一个单位（总资产或营业收入过亿就用亿元）。"""
    bs_cols, flow_cols = col_labels(period, year)
    tables = {k: _Table(st[k]) for k in (BS, IS, CF) if (st.get(k) or {}).get("ok")}
    size = max([abs(t.get(k) or 0) for t, k in ((tables.get(BS), "total_assets"), (tables.get(IS), "revenue"))
                if t is not None] or [0])
    scale, unit = _scale(size)
    sections = []
    if BS in tables:
        sections.append(_bs_section(tables[BS], bs_cols, scale))
    revenue = None
    if IS in tables:
        sections.append(_is_section(tables[IS], flow_cols, scale))
        revenue = tables[IS].get("revenue") or tables[IS].get("total_rev")
    if CF in tables:
        sections.append(_cf_section(tables[CF], flow_cols, scale, revenue))
    for s in sections:
        s["checks"] = [c["desc"] for c in st[s["key"]].get("checks") or [] if c.get("ok")]
    skipped = [{"key": k, "name": NAMES[k], "why": (st.get(k) or {}).get("why") or "没找到这张表"}
               for k in (BS, IS, CF) if k not in tables]
    return {"period": period, "unit": unit, "sections": sections, "skipped": skipped}


def brief(c: dict, label: str) -> str:
    """回答正文：三段导语（各带页码），一段核对说明，没出的表说清原因。细节在卡片里。"""
    src = f"《{label}》" if label else "这份报告"
    secs = c.get("sections") or []
    if not secs:
        whys = "；".join(f"{s['name']}：{s['why']}" for s in c.get("skipped") or [])
        return f"没能从{src}里读出能核对上的三张合并报表（{whys}）。可以直接问具体科目，比如「货币资金有多少」。"
    paras = [f"按{src}里的合并报表，三表速读如下（卡片里的金额单位：{c['unit']}）。"]
    for s in secs:
        paras.append(f"**{NAMES[s['key']]}**（第 {s['page']} 页）：{s['lead']}")
    n = sum(len(s.get("checks") or []) for s in secs)
    if n:
        paras.append(f"**核对**：{n} 条勾稽关系（比如「{secs[0]['checks'][0]}」）本期、上期两列都对上了，"
                     "卡片里每张表下面列着核了哪几条。")
    for s in c.get("skipped") or []:
        paras.append(f"**{s['name']}**没有展示：{s['why']}。对不上账的数宁可不出。")
    if c.get("period", FY) != FY:
        paras.append("季报、半年报一般未经审计；利润表和现金流量表是年初到报告期末的累计数。")
    paras.append("速读只讲结构、不判断好坏，不构成投资建议；想看风险判断，可以点「财报体检」。点卡片上的页码可以翻到原页核对。")
    return "\n\n".join(paras)


# ---------------------------------------------------------------- 回合

_WANTS_RE = re.compile(r"三表|三张(?:财务)?(?:报)?表|三大(?:财务)?报表|三大表|报表速读|速读报表|"
                       r"(?:看懂|读懂|解读|拆解|速读)(?:一下)?(?:这份|它的|公司的?)?(?:财务)?报表")


def wants_statements(question: str) -> bool:
    return bool(_WANTS_RE.search(question or ""))


def button_question(company: str) -> str:
    """「三表速读」按钮发出的问题。写成自然语言，聊天记录里读得懂，也能被 wants_statements 认出。"""
    who = company or "这家公司"
    return f"三表速读：{who}的资产负债表、利润表、现金流量表各说明了什么"


# ---------------------------------------------------------------- 片段：三张表进回答（第二步）
#
# 普通提问问到负债率、有息负债、自由现金流这些时，模型只能从检索到的报表原文里自己找数、自己算，
# 只有一列有数的行、折行的科目都容易读错列。这里把勾稽核对过的数做成片段接在检索片段后面：
#   · 每张表一个片段：报表原值（和卡片同一批数），可以记成事实笔记；
#   · 一个「程序计算的指标」片段：有息负债、资产负债率、毛利率、自由现金流这些按公式算的数，
#     标 derived，不记成笔记（和 rag/quarters 的单季数一样），回答要说明是按公式计算的。
# 只在两种情况下接（用户 2026-11-03 定）：问题问到这些科目或指标；上一轮是三表速读（追问）。

STMT_SECTION = "三表数据"          # 片段标题；rag/generator.STMT_RULE 按这个名字讲怎么用
DERIVED_TITLE = "程序计算的指标"

_ITEM_Q_RE = re.compile(
    r"负债率|负债|有息|借款|债务|偿债|货币资金|现金|应收|存货|应付|预收|合同负债|上下游|占款|"
    r"毛利|费用率|期间费用|销售费用|管理费用|研发费用|财务费用|净利率|减值|所得税|"
    r"现金流|资本开支|自由现金|收现|分红|付息|筹资|投资活动|经营活动|总资产|净资产|资产负债表|利润表|现金流量表")


def wants_items(question: str, last_kind: str | None = None) -> bool:
    """这一问要不要接三表片段：上一轮是三表速读（「应付为什么涨了」这种追问），或问到了表里的科目、指标。"""
    return last_kind == "statements" or bool(_ITEM_Q_RE.search(question or ""))


def _cells(cols, cur, prev, fmt) -> list[str]:
    return [f"{c} {fmt(v)}" for c, v in zip(cols, (cur, prev)) if v is not None]


def _note_amount(v: float) -> str:
    """和事实笔记同一种写法（rag/notes._amount）：逐格核对按「亿元 / 万元」找数。"""
    return f"{v / 1e8:,.2f} 亿元" if abs(v) >= 1e8 else f"{v / 1e4:,.2f} 万元"


def _pct_of(v: float) -> str:
    return f"{v:.1f}%"


def as_hits(st: dict | None, doc_id: str, label: str, period: str = FY, year="") -> list[dict]:
    """核对过的三张表 → 片段（每张表一个 + 程序计算的指标一个）。勾稽没对上的表不出片段。"""
    if not st or not doc_id:
        return []
    bs_cols, flow_cols = col_labels(period, year)
    tables = {k: _Table(st[k]) for k in (BS, IS, CF) if (st.get(k) or {}).get("ok")}
    out = []
    for k, t in tables.items():
        cols = bs_cols if k == BS else flow_cols
        lines = []
        for key, aliases in ITEMS[k].items():
            it = t.items.get(key)
            if not it or key in ("interest_net", "premium"):
                continue
            cells = _cells(cols, it.get("cur"), it.get("prev"), _note_amount)
            if not cells:
                continue
            c = _chg(it.get("cur"), it.get("prev"), 1.0)
            if c.endswith("%"):
                cells.append(f"增减 {c}")
            lines.append(f"{aliases[0]}：{'；'.join(cells)}")
        pages = [it["page"] for it in t.items.values() if it.get("page")] or [t.page]
        out.append({"doc_id": doc_id, "note_src": label, "text": "\n".join(lines),
                    "section": f"{STMT_SECTION} · {NAMES[k]}", "subsection": "合并报表（勾稽已核对）",
                    "page_start": int(min(pages)), "page_end": int(max(pages)), "stmt": k})
    derived = _derived_lines(tables, bs_cols, flow_cols)
    if derived:
        first = min(h["page_start"] for h in out)
        out.append({"doc_id": doc_id, "note_src": label, "derived": True, "stmt": "derived",
                    "text": "\n".join(["以下指标由程序按括号里的公式、用本报告合并报表的数算出，不是公司披露的数"] + derived),
                    "section": f"{STMT_SECTION} · {DERIVED_TITLE}", "subsection": "按公式计算",
                    "page_start": first, "page_end": first})
    return out


def _derived_lines(tables: dict, bs_cols, flow_cols) -> list[str]:
    lines = []

    def add(name: str, formula: str, cols, cur, prev, fmt, chg: str = ""):
        cells = _cells(cols, cur, prev, fmt)
        if cells:
            lines.append(f"{name}（{formula}）：{'；'.join(cells + ([chg] if chg else []))}")

    def amount(name, formula, cols, cur, prev):
        c = _chg(cur, prev, 1.0)
        add(name, formula, cols, cur, prev, _note_amount, f"增减 {c}" if c.endswith("%") else "")

    def ratio(name, formula, cols, cur, prev):
        add(name, formula, cols, cur, prev, _pct_of, f"变化 {_pp(cur, prev)}" if _pp(cur, prev) else "")

    t = tables.get(BS)
    if t:
        ratio("资产负债率", "负债合计 ÷ 资产总计", bs_cols, _ratio(t.get("total_liab"), t.get("total_assets")),
              _ratio(t.get("total_liab", "prev"), t.get("total_assets", "prev")))
        amount("有息负债", DEBT_NOTE, bs_cols, t.total(DEBT_KEYS), t.total(DEBT_KEYS, "prev"))
        amount("应收合计", REC_NOTE, bs_cols, t.total(REC_KEYS), t.total(REC_KEYS, "prev"))
        amount("应付和预收合计", PAY_NOTE, bs_cols, t.total(PAY_KEYS), t.total(PAY_KEYS, "prev"))
    t = tables.get(IS)
    revenue = None
    if t:
        rev, rev0 = t.get("revenue") or t.get("total_rev"), t.get("revenue", "prev") or t.get("total_rev", "prev")
        revenue = rev
        cost, cost0 = t.get("cost"), t.get("cost", "prev")
        ratio("毛利率", "(营业收入 − 营业成本) ÷ 营业收入", flow_cols,
              _ratio(rev - cost, rev) if rev and cost is not None else None,
              _ratio(rev0 - cost0, rev0) if rev0 and cost0 is not None else None)
        ratio("期间费用率", "销售费用 + 管理费用 + 研发费用 + 财务费用，除以营业收入", flow_cols,
              _ratio(t.total(FEE_KEYS), rev), _ratio(t.total(FEE_KEYS, "prev"), rev0))
        ratio("净利率", "净利润 ÷ 营业收入", flow_cols, _ratio(t.get("net_profit"), rev),
              _ratio(t.get("net_profit", "prev"), rev0))
        for col, name in (("cur", flow_cols[0]), ("prev", flow_cols[1])):
            flow = per_hundred(t, col)
            if flow:
                parts = "、".join(f"{x['label']} {x['v']:.1f} 元" for x in flow if not x.get("net"))
                lines.append(f"每 100 元营业收入的去向（{name}，其他收支按净利润倒算，负数为净收入）："
                             f"{parts}，{_left(flow[-1]['v'])}")
    t = tables.get(CF)
    if t:
        op, capex = t.get("op_net"), t.get("capex")
        op0, capex0 = t.get("op_net", "prev"), t.get("capex", "prev")
        if capex is not None:
            add("自由现金流", "经营活动产生的现金流量净额 − 购建固定资产、无形资产和其他长期资产支付的现金", flow_cols,
                op - capex, None if capex0 is None or op0 is None else op0 - capex0, _note_amount)
        if revenue and t.get("sales_cash") is not None:
            ratio("收现比", "销售商品、提供劳务收到的现金 ÷ 营业收入，现金含增值税", flow_cols,
                  _ratio(t.get("sales_cash"), revenue),
                  _ratio(t.get("sales_cash", "prev"), tables[IS].get("revenue", "prev") or tables[IS].get("total_rev", "prev")))
        sign = "".join("+" if v >= 0 else "-" for v in (op, t.get("inv_net"), t.get("fin_net")))
        lines.append(f"现金流画像（{flow_cols[0]}，按经营、投资、筹资三项净额的正负）：{PORTRAITS[sign]}")
    return lines


def attach(hits: list[dict], new: list[dict] | None) -> list[dict]:
    """三表片段接到最后；旧的（复核回退前那一版）换掉。"""
    if not new:
        return list(hits)
    return [h for h in hits if not str(h.get("section") or "").startswith(STMT_SECTION + " · ")] + list(new)
