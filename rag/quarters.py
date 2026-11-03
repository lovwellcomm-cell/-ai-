"""多年趋势第二步：最近 8 个季度的单季序列（指标卡画单季折线）。

定期报告只披露累计数：一季报 1–3 月、半年报 1–6 月、三季报 1–9 月（外加第三季度单季）、年报全年。
单季靠同一年的累计数相减：Q2 = 半年报 − 一季报，Q3 = 三季报 − 半年报，Q4 = 年报 − 三季报。
每份报告还带一列「上年同期」，所以一份报告能给出两年的同一期累计数。

要哪几份报告（以最新一期是 Y 年三季报为例）：Y 年一季报、半年报、三季报给出 Y 和 Y−1 年的前三期累计，
Y−1 年年报给出 Y−1、Y−2 年全年，Y−1 年三季报给出 Y−2 年前三季度 —— 合起来是 Y−2 年 Q4 到 Y 年 Q3 正好 8 季。
一般地：Y 年截至最新一期的各份 + Y−1 年从同一期到年报的各份，最多 5 份（最新是年报时 4 份）。
不在本地的去巨潮只抽表（rag/history.load_report，全站缓存），不建索引、不占导入额度。

三季报直接披露的第三季度单季数优先用（rag/financials 已核对过列序），其余都是相减得来的，卡片上写明「据累计数相减」。
同一期累计数既有那份报告自己的，也有下一年报告里的「上年同期」（可能追溯调整过）：先用报告自己的。
"""
from __future__ import annotations

import re

from .financials import FLOW_KEYS
from .periods import FY, H1, PERIODS, Q1, Q3, report_label

N_QUARTERS = 8
MIN_QUARTERS = 5          # 连续算得出的季度少于这个数就不画（退回原来的本期 / 上年同期柱子）
KEYS = ("revenue", "np_parent")


def needed_reports(year: int, period: str) -> list[tuple[int, str]]:
    """拼到 (year, period) 为止的 8 季要哪几份报告，新的在前。"""
    k = PERIODS.index(period) + 1
    own = [(year, p) for p in PERIODS[:k]]
    prev = [(year - 1, p) for p in PERIODS[k - 1:]] if k < 4 else []
    return list(reversed(own)) + list(reversed(prev))


def cumulative(fins: list[dict]) -> tuple[dict, dict]:
    """各份报告 → ({(年, 期, 科目): 累计数}, {(年, 科目): 三季报披露的第三季度单季数})。

    先填各报告自己那一年的数，再用「上年同期」列补没有的。
    """
    cum: dict[tuple[str, str, str], float] = {}
    q3_single: dict[tuple[str, str], float] = {}
    for own in (True, False):
        for fin in fins:
            period = fin.get("period") or FY
            years = fin.get("years") or []
            cols = years[:1] if own else years[1:]
            for key in FLOW_KEYS:
                vals = ((fin.get("items") or {}).get(key) or {}).get("values") or {}
                for y in cols:
                    if isinstance(vals.get(y), (int, float)):
                        cum.setdefault((y, period, key), vals[y])
                q = ((fin.get("items") or {}).get(key) or {}).get("quarter") or {}
                if own and period == Q3 and years and isinstance(q.get("value"), (int, float)):
                    q3_single[(years[0], key)] = q["value"]
    return cum, q3_single


def single(cum: dict, q3_single: dict, year: str, q: int, key: str) -> float | None:
    """year 年第 q 季度的单季数；缺任何一个累计数就返回 None。"""
    if q == 3 and (year, key) in q3_single:
        return q3_single[(year, key)]
    cur = cum.get((year, PERIODS[q - 1], key))
    if cur is None:
        return None
    if q == 1:
        return cur
    before = cum.get((year, PERIODS[q - 2], key))
    return None if before is None else cur - before


def quarter_list(year: int, period: str, n: int = N_QUARTERS) -> list[tuple[str, int]]:
    """截至 (year, period) 的最近 n 个季度，旧 → 新。"""
    y, q = year, PERIODS.index(period) + 1
    out = []
    for _ in range(n):
        out.append((str(y), q))
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    return list(reversed(out))


def series(fins: list[dict], year: int, period: str) -> dict | None:
    """拼单季序列。每个科目只取截到最新一季的连续一段（中间缺一季就从缺口之后算起），不够 MIN_QUARTERS 季不要。"""
    cum, q3s = cumulative(fins)
    quarters = quarter_list(year, period)
    items = {}
    for key in KEYS:
        vals = [single(cum, q3s, y, q, key) for y, q in quarters]
        run = 0
        while run < len(vals) and vals[len(vals) - 1 - run] is not None:
            run += 1
        if run >= MIN_QUARTERS:
            items[key] = {"labels": [f"{y[2:]}Q{q}" for y, q in quarters[-run:]], "values": vals[-run:]}
    if not items:
        return None
    used = sorted({(int(f["years"][0]), f.get("period") or FY) for f in fins if f.get("years")},
                  key=lambda t: (t[0], PERIODS.index(t[1])))
    return {"anchor": report_label(year, period), "items": items,
            "sources": [report_label(y, p) for y, p in used]}


def build(company: str, year: int, period: str, docs: list[dict], *, budget: int,
          load) -> tuple[dict | None, int, list[str]]:
    """取齐要的报告并拼序列。load = rag/history.load_report（测试里换掉）。

    返回 (序列或 None, 这次真的下载了几份, 出错的报告)。下载额度用完后只看本地和缓存；
    某一份下载失败不影响别的，缺了它的那几季就不画。
    """
    fins, fetched, errors = [], 0, []
    for y, p in needed_reports(year, period):
        try:
            fin, did = load(company, y, p, docs, allow_fetch=fetched < budget)
        except Exception as e:
            errors.append(f"{report_label(y, p)}：{type(e).__name__}: {e}")
            continue
        fetched += int(did)
        if fin:
            fins.append(fin)
    return series(fins, year, period), fetched, errors


_QUARTER_RE = re.compile(r"单季|分季度?|各季度?|每(一)?个?季度?|逐季|季度(变化|走势|趋势|数据|表现|情况)|"
                         r"(近|最近|过去)\s*[几两二三四五六七八九十2-9]\s*个?季度?")


def wants_quarters(question: str, previous: list[str] | tuple[str, ...] = ()) -> bool:
    """年报上点名要季度数据（「最近几个季度营收」「单季利润」）时也拼单季序列。短追问跟着上一问走。"""
    from .history import _FOLLOWUP_RE, _TREND_RE

    q = (question or "").strip()
    # 「近几年营收趋势如何」也够短、像追问，但它自己说了要看几年：不继承上一问的「最近几个季度」（本地实测踩过）
    if previous and _FOLLOWUP_RE.match(q) and not _QUARTER_RE.search(q) and not _TREND_RE.search(q):
        *earlier, last = previous
        return wants_quarters(last, earlier)
    return bool(_QUARTER_RE.search(q))


QUARTER_SECTION = "单季数据"            # 片段标题；rag/generator.NOTES_RULE 按这个名字讲怎么用
_CN_Q = "一二三四"


def as_hit(s: dict | None, doc_id: str, page: int, company: str) -> dict | None:
    """单季序列 → 片段，让回答也能说「最近 8 季」（不然卡片画了 8 季，正文却说「未单独披露」，本地实测踩过）。

    数是程序相减算的，不在任何一页原文里：翻原页落在当前报告的主要会计数据页；derived 标记让它不被记成事实笔记
    （rag/notes.from_answer），免得下一轮把推算数当原文数字。
    """
    from .financials import LABELS
    from .notes import _amount

    if not s or not s.get("items") or not doc_id or not page:
        return None
    lines = [f"以下单季数由程序据{'、'.join(s['sources'])}的累计数相减得出（第三季度为三季报披露的单季数），未经公司单独披露"]
    for key, item in s["items"].items():
        cells = [f"{2000 + int(lab[:2])} 年第{_CN_Q[int(lab[-1]) - 1]}季度 {_amount(v)}"
                 for lab, v in zip(item["labels"], item["values"])]
        lines.append(f"{LABELS[key]}（单季）：{'；'.join(cells)}")
    src = f"{company} {s['anchor']}".strip()
    return {"doc_id": doc_id, "note_src": src, "note": True, "derived": True, "text": "\n".join(lines),
            "section": f"{QUARTER_SECTION} · 最近 {max(len(i['values']) for i in s['items'].values())} 季",
            "subsection": "据累计数相减", "page_start": int(page), "page_end": int(page)}


def attach(hits: list[dict], hit: dict | None) -> list[dict]:
    """单季片段接到最后，旧的（复核回退前那一版）换掉。它和检索到的主要会计数据同页，所以不能按页去重。"""
    if not hit:
        return list(hits)
    return [h for h in hits if not h.get("derived")] + [hit]
