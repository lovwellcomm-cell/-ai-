"""跨口径计算：回答把不同口径的两个数直接相减或算增长率（跨报告回答第三期）。

分步读两份报告之后，片段里同时有「2025 年营业收入 246.74 亿元」（全年）和「2026 年前三季度 180.00 亿元」
（九个月累计）。模型照样会写「同比下降 27.05%」—— 拿九个月去比十二个月，数字每一位都有出处，
逐格核对和复核模型都看不出错。生成提示词里早就写了不许这么算，光靠提示词拦不住。

做法：只看**有结构的数** —— 主报告的主要会计数据（rag/notes.from_financials 的格式）和事实笔记，
每条带科目、年份、口径（全年 / 一季度 / 上半年 / 前三季度 / 单季）。回答里每个在片段里找不到原样出处
（即「算」出来的）的百分比或金额，拿同一科目、口径不同的两个数去凑：

  · 增长率凑得上 → 一定错（九个月比十二个月，没有合法的读法），硬判；
  · 差额凑得上、年份不同 → 一定错（2026 前三季度减 2025 全年），硬判；
  · 差额凑得上、同一年 → 可能是合法的推算（全年减前三季度 = 第四季度单季），只作线索交给复核模型。

时点数（总资产、净资产，「年末」「季末」）不查：期末对期末本来就能比。
只认同一科目：不限科目的话，几百对数字里总能碰巧凑出一个百分比。
"""
from __future__ import annotations

import re

from .fidelity import _found, _hits_index, _tolerance, extract_numbers, table_cells
from .financials import _ALIAS
from .periods import FY, H1, Q1, Q3, SHORT

MAX_FINDINGS = 3

_SRC_PERIOD_RE = re.compile("|".join(SHORT[p] for p in (Q1, H1, Q3)) + "|年报")
_SPAN_RES = (("单季", re.compile(r"单季")),
             (Q3, re.compile(r"前三季度|1\s*[-–—至~]\s*9\s*月")),
             (H1, re.compile(r"上半年|半年度|1\s*[-–—至~]\s*6\s*月")),
             (Q1, re.compile(r"一季度|1\s*[-–—至~]\s*3\s*月")),
             (FY, re.compile(r"全年|年度|年报")))
_POINT_RE = re.compile(r"年末|季末|期末|月末")
_FLOW_RE = re.compile(r"收入|利润|收益|成本|费用|现金流")
_YEAR_RE = re.compile(r"(?<![\d.,])(20\d\d)(?![\d.,])")
_LABEL_UNIT_RE = re.compile(r"[（(][^）)]*[)）]")
_SPAN_NAME = {FY: "全年", Q1: "一季度", H1: "上半年", Q3: "前三季度", "单季": "单季"}


def _src_period(src: str) -> str | None:
    m = _SRC_PERIOD_RE.search(src or "")
    if not m:
        return None
    return {SHORT[Q1]: Q1, SHORT[H1]: H1, SHORT[Q3]: Q3}.get(m.group(0), FY)


def _span(seg: str, default: str | None) -> str | None:
    for span, rx in _SPAN_RES:
        if rx.search(seg):
            return span
    return default


def _label_key(label: str) -> str:
    plain = _LABEL_UNIT_RE.sub("", re.sub(r"\s+", "", label))
    return _ALIAS.get(plain, plain)


def facts(notes: list[dict]) -> list[dict]:
    """笔记（{"src", "t"}，t 形如「营业收入：2025 年 246.74 亿元；…」）→ 带口径的流量数。

    每段的口径先看段里写没写（「前三季度」「单季」），没写就按这份报告的期：年报的「2025 年」是全年，
    三季报笔记里没写口径的段（回答表格的列头常写「本期」）就是前三季度累计。年份同理，段里没写取报告的年份。
    """
    out = []
    for n in notes or []:
        label, sep, body = (n.get("t") or "").partition("：")
        if not sep or not _FLOW_RE.search(label):
            continue
        src = n.get("src") or ""
        src_year = (m.group(1) if (m := _YEAR_RE.search(src)) else None)
        default = _src_period(src)
        for seg in body.split("；"):
            if _POINT_RE.search(seg) or "增减" in seg:
                continue
            amounts = [x for x in extract_numbers(re.sub(r"[（(][^）)]*[)）]", "", seg))
                       if x["kind"] == "amount" and x["unit"]]
            span = _span(seg, default)
            if not amounts or span is None:
                continue
            y = (m.group(1) if (m := _YEAR_RE.search(seg)) else src_year)
            out.append({"key": _label_key(label), "label": label.strip(), "src": src, "year": y,
                        "span": span, "value": amounts[0]["value"], "text": amounts[0]["text"]})
    return out


def _desc(f: dict) -> str:
    year = f"{f['year']} 年" if f.get("year") else ""
    return f"{f['src']}：{f['label']} {year}{_SPAN_NAME.get(f['span'], '')} {f['text']}".strip()


def _answer_numbers(answer: str) -> list[dict]:
    """回答里的百分比和带单位的金额；表格里的裸数字按表头单位补全（rag/fidelity.table_cells）。"""
    seen, out = set(), []
    texts = [answer or ""] + [check for check, _ in table_cells(answer)]
    for t in texts:
        for x in extract_numbers(t):
            if x["kind"] == "year" or (x["kind"] == "amount" and not x["unit"]):
                continue
            k = (x["kind"], x["text"])
            if k not in seen:
                seen.add(k)
                out.append(x)
    return out


def cross_span(answer: str, hits: list[dict], fact_list: list[dict]) -> list[dict]:
    """回答里由不同口径的两个数算出来的百分比 / 差额。

    返回 [{"num", "how", "a", "b", "hard"}]：how 是「增长率」或「差额」，a / b 是两个数的出处说明，
    hard 为真表示一定错（见模块说明），为假只是线索。
    """
    if len(fact_list) < 2:
        return []
    index = _hits_index(hits or [])
    pairs = [(a, b) for i, a in enumerate(fact_list) for b in fact_list[i + 1:]
             if a["key"] == b["key"] and a["span"] != b["span"]]
    if not pairs:
        return []
    out = []
    for x in _answer_numbers(answer):
        if x["kind"] == "percent":
            # 只认片段里原样出现的；fidelity._percent_ok 还认「由片段金额算得出」，跨口径的增长率正好算得出
            if x["raw"].replace(",", "") in index["percents"]:
                continue
            tol = 0.5 * 10 ** -x["dec"] + 1e-9
            for a, b in pairs:
                if any(d["value"] and abs(abs((c["value"] - d["value"]) / d["value"] * 100) - x["value"]) <= tol
                       for c, d in ((a, b), (b, a))):
                    out.append({"num": x["text"], "how": "增长率", "a": _desc(a), "b": _desc(b), "hard": True})
                    break
        else:
            if _found(x["value"], x["dec"], x.get("scale", 1.0), index):
                continue
            tol = _tolerance(x["value"], x["dec"], x.get("scale", 1.0))
            for a, b in pairs:
                if abs(abs(a["value"] - b["value"]) - x["value"]) <= tol:
                    out.append({"num": x["text"], "how": "差额", "a": _desc(a), "b": _desc(b),
                                "hard": a["year"] != b["year"]})
                    break
        if len(out) >= MAX_FINDINGS:
            break
    return out


def note_lines(hits: list[dict]) -> list[dict]:
    """事实笔记片段（rag/notes.as_hits，每行一条笔记）→ 笔记形状，交给 facts()。补充检索的原文片段不算。"""
    return [{"src": h.get("note_src") or "", "t": line}
            for h in hits or [] if h.get("note") and not h.get("extra")
            for line in (h.get("text") or "").splitlines() if line.strip()]
