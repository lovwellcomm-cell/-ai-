"""事实笔记：把前几轮核对过的数字带进这一轮（跨报告回答第一期）。

用户的例子：先问「近三年发展态势如何」（依据 2025 年报），再问「结合最新季报对比一下」→ 这一轮换到
三季报上答，年报的数字一个也看不到，只能说「年报中未找到」。一轮只检索一份报告，而用户要的是上下文联动。

做法：每轮结束时从两处抽笔记，**只记核对过的数字**，每条带 doc_id + 页码：
  · 指标卡用的主要会计数据（rag/financials.py 程序抽取、按披露增减复算过）；
  · 回答表格里逐格核对通过的行（rag/fidelity.unverified_in_tables 没标出的单元格）。
不加模型调用。下一轮把同一段对话最近 3 轮里**来自别的报告**的笔记，按「报告 + 页码」合并成最多 4 个
额外片段，接在检索片段后面编号。这样引用 [n]、逐格核对、复核、来源列表、翻原页都照常工作 ——
片段自带 doc_id，翻原页时翻的是那份报告。

笔记里的数字在下一轮又会被核对、再被抽成笔记：出处沿用它原来那份报告的页码，不会变成「前几轮」。
"""
from __future__ import annotations

import re

from .fidelity import (_CITE_RE, _PP_RE, _SEPARATOR_RE, _TABLE_LINE_RE, _found, _hits_index,
                       _split_row, extract_numbers)
from .company import same_company
from .financials import BALANCE_KEYS
from .periods import FY, RANK, SPAN, period_of, report_label

NOTE_TURNS = 3            # 最近几轮的笔记带进下一轮（用户 2026-11-03 定）
MAX_PER_TURN = 20         # 每轮最多记几条（一条 = 表格一行 / 一个科目）；分步读两三份报告时要够放
MAX_NOTE_HITS = 4         # 带进下一轮的额外片段最多几个（每个 = 一份报告的一页）
NOTE_SECTION = "前几轮已核对的数据"
TEXT_LIMIT = 300

_CITE_NUM_RE = re.compile(r"\[([\d,，、\s]+)\]")
_BOLD_RE = re.compile(r"\*\*")


def _amount(v: float) -> str:
    return f"{v / 1e8:,.2f} 亿元" if abs(v) >= 1e8 else f"{v / 1e4:,.2f} 万元"


def from_financials(fin: dict | None, doc_id: str, src: str) -> list[dict]:
    """主要会计数据 → 每个科目一条笔记。fin 必须是这份报告自己的（不能是拼了往年年报的六年数据）。"""
    if not fin or not fin.get("page") or not doc_id:
        return []
    period = fin.get("period") or FY
    years = fin.get("years") or []

    def col(key: str, y: str, i: int) -> str:
        if period == FY:
            return f"{y} 年"
        if key in BALANCE_KEYS:
            return f"{y} 年{SPAN[period]}末" if i == 0 else f"{y} 年末"
        return f"{y} 年{SPAN[period]}"

    out = []
    for key, item in (fin.get("items") or {}).items():
        vals = item.get("values") or {}
        cells = [f"{col(key, y, i)} {_amount(vals[y])}" for i, y in enumerate(years) if y in vals]
        if not cells:
            continue
        if item.get("change") is not None:
            cells.append(f"本期比上年同期增减 {item['change']:+.2f}%")
        # 三季报的第三季度单季数（rag/financials 放在 quarter 里）：问「一季度和三季度」时要的就是它
        q = item.get("quarter") or {}
        if period != FY and isinstance(q.get("value"), (int, float)) and years:
            cells.append(f"{years[0]} 年第三季度单季 {_amount(q['value'])}"
                         + (f"（同比 {q['change']:+.2f}%）" if isinstance(q.get("change"), (int, float)) else ""))
        out.append({"doc": doc_id, "src": src, "p": int(fin["page"]),
                    "t": f"{item.get('label', key)}：{'；'.join(cells)}"[:TEXT_LIMIT]})
    return out


def _source_hit(numbers: list[dict], cited: list[int], hits: list[dict]) -> dict | None:
    """这一行的数字出自哪个片段：先看这一行引用的片段，再看全部片段；要能在片段里**原样**找到金额。"""
    order = [hits[n - 1] for n in cited if 1 <= n <= len(hits)]
    order += [h for h in hits if h not in order]
    amounts = [n for n in numbers if n["kind"] == "amount"]
    for h in order:
        index = _hits_index([h])
        if any(_found(n["value"], n["dec"], n.get("scale", 1.0), index) for n in amounts):
            return h
    # 整行只有百分比（毛利率、占比）：只认这一行自己引用的片段
    if not amounts and cited and 1 <= cited[0] <= len(hits):
        return hits[cited[0] - 1]
    return None


def from_answer(answer: str, hits: list[dict], unverified: list[str], doc_id: str,
                src: str) -> list[dict]:
    """回答里的 Markdown 表格 → 每行一条笔记，丢掉逐格核对没通过的单元格。

    出处取这一行数字所在的片段：本轮检索的片段 → 这份报告（doc_id）；
    带进来的笔记片段 → 沿用它自己的报告和页码。
    """
    out: list[dict] = []
    block: list[str] = []
    bad = [u for u in unverified or [] if u]

    def flush():
        if len(block) < 3 or not _SEPARATOR_RE.match(block[1]):
            block.clear()
            return
        head = _split_row(block[0])
        for line in block[2:]:
            cols = _split_row(line)
            if len(cols) < 2:
                continue
            label = _BOLD_RE.sub("", cols[0]).strip()
            cited = [int(x) for m in _CITE_NUM_RE.finditer(line)
                     for x in re.split(r"[,，、\s]+", m.group(1)) if x.isdigit()]
            cells, numbers = [], []
            for j, cell in enumerate(cols[1:], start=1):
                h = head[j] if j < len(head) else ""
                text = _CITE_RE.sub("", cell).replace("**", "").strip()
                if not text or "来源" in h or any(b in cell for b in bad):
                    continue
                # 「减少 0.13 个百分点」的 0.13 不是金额：不去掉的话会拿它去片段里找「0.13 元」，
                # 找不到就把整行（毛利率、ROE）当成没出处丢掉（线上实测踩过）
                nums = [n for n in extract_numbers(_PP_RE.sub(" ", text)) if n["kind"] != "year"]
                if not nums and _PP_RE.search(text):
                    cells.append(f"{_BOLD_RE.sub('', h).strip()} {text}".strip())
                    continue
                if not nums:
                    continue
                numbers += nums
                cells.append(f"{_BOLD_RE.sub('', h).strip()} {text}".strip())
            if not label or not cells:
                continue
            hit = _source_hit(numbers, cited, hits)
            if hit is None:
                continue
            out.append({"doc": hit.get("doc_id") or doc_id,
                        "src": hit.get("note_src") or src,
                        "p": int(hit.get("page_start") or 0),
                        "t": f"{label}：{'；'.join(cells)}"[:TEXT_LIMIT]})
        block.clear()

    for line in (answer or "").splitlines():
        if _TABLE_LINE_RE.match(line):
            block.append(line)
        else:
            flush()
    flush()
    return [n for n in out if n["doc"] and n["p"] > 0]


_AMOUNT_RE = re.compile(r"-?\d[\d,]*\.\d+\s*(?:亿元|万元)")


def _amounts(text: str) -> set[str]:
    return {re.sub(r"\s+", "", m.group(0)) for m in _AMOUNT_RE.finditer(text)}


def merge(*groups: list[dict]) -> list[dict]:
    """去重并截到 MAX_PER_TURN 条。

    除了一字不差的重复，还去掉「换个说法的重复」：指标卡记了「营业收入：2025 年 246.74 亿元…」，
    回答表格那一行又是同样三个金额 —— 金额全都已经出现在同一页某条笔记里的，不再占名额。
    只有比率、没有金额的行（毛利率、ROE）照留。先传进来的组优先（指标卡的数字是程序复算过的）。
    """
    seen, out = set(), []
    for n in (x for g in groups for x in g or []):
        key = (n["doc"], n["p"], n["t"])
        if key in seen:
            continue
        mine = _amounts(n["t"])
        if mine and any(o["doc"] == n["doc"] and o["p"] == n["p"] and mine <= _amounts(o["t"])
                        for o in out):
            continue
        seen.add(key)
        out.append(n)
    return out[:MAX_PER_TURN]


def as_hits(history: list[dict], current_doc: str, turns: int = NOTE_TURNS,
            extra: list[dict] | None = None) -> list[dict]:
    """最近几轮里来自**别的报告**的笔记 → 额外片段（同一报告同一页合成一个）。

    当前报告自己的数字不带：检索本来就在这份报告里找，再带一遍只是重复。
    extra 是这一轮前置步骤刚记下的笔记（第二期：先读别的报告），排在最前面。
    越新的轮次越靠前：片段有上限时先留最近的。
    """
    pages: dict[tuple[str, int], dict] = {}
    recent = list(reversed(history[-turns:] if turns > 0 else []))
    for e in ([{"notes": extra}] if extra else []) + recent:
        for n in e.get("notes") or []:
            if n.get("doc") == current_doc:
                continue
            key = (n["doc"], n["p"])
            if key not in pages:
                if len(pages) >= MAX_NOTE_HITS:
                    continue
                pages[key] = {"doc_id": n["doc"], "note_src": n["src"], "lines": [],
                              "section": f"{NOTE_SECTION} · {n['src']}", "subsection": "",
                              "page_start": n["p"], "page_end": n["p"]}
            if n["t"] not in pages[key]["lines"]:
                pages[key]["lines"].append(n["t"])
    hits = []
    for h in pages.values():
        lines = h.pop("lines")
        hits.append(dict(h, text="\n".join(lines), note=True))
    return hits


def other_reports(docs: list[dict], current: dict, current_id: str, limit: int = 6) -> dict[str, str]:
    """{「2026 半年报」: doc_id}：同一公司本地已有的别的报告，新的在前 —— 复核器只能从这几份里挑补检索的去处。"""
    company = current.get("company", "")
    if not company:
        return {}
    mine = [d for d in docs or []
            if d.get("doc_id") != current_id and same_company(d.get("company", ""), company)
            and str(d.get("year", "")).isdigit()]
    mine.sort(key=lambda d: (int(d["year"]), RANK[period_of(d)]), reverse=True)
    out: dict[str, str] = {}
    for d in mine:
        out.setdefault(report_label(d["year"], period_of(d)), d["doc_id"])
    return dict(list(out.items())[:limit])


def as_supplement(found: list[dict], sdoc: str, src: str, k: int) -> list[dict]:
    """第三期：复核要求去别的报告补检索时，那份报告检索到的前 k 段原文 → 额外片段。

    和笔记片段一样带 doc_id、note、note_src：引用、逐格核对、翻原页、记成笔记都照常；
    extra 标明是原文不是笔记（rag/caliber.note_lines 不把它当带口径的数）。原来的章节名挪到 subsection。
    """
    return [dict(h, doc_id=sdoc, note=True, extra=True, note_src=src, section=f"补充检索 · {src}",
                 subsection=" · ".join(x for x in (h.get("section"), h.get("subsection")) if x))
            for h in (found or [])[:k]]


def sources_line(entry: dict) -> str:
    """规划器看的一句话：这一轮记下了哪些报告的哪些数据（只列报告和科目名，不带数字）。"""
    by_src: dict[str, list[str]] = {}
    for n in entry.get("notes") or []:
        name = n["t"].split("：", 1)[0]
        items = by_src.setdefault(n["src"], [])
        if name not in items:
            items.append(name)
    return "；".join(f"{src}（{'、'.join(items[:6])}）" for src, items in by_src.items())


# ---------------------------------------------------------------- 第二期：前置步骤

# 主要会计数据表覆盖得到的科目：前置步骤要的数据都在这里面时，只用程序抽的数，不调模型
_FIN_NEED_RE = re.compile(r"营收|营业收入|收入|净利|利润|扣非|现金流|总资产|净资产|业绩|财务|经营情况|发展")


def covered_by_financials(need: list[str]) -> bool:
    """前置步骤要的数据是否都能从主要会计数据里拿到。没写要什么（need 为空）也按财务数据处理。"""
    return all(_FIN_NEED_RE.search(n) for n in need or [])


def prestep_question(company: str, label: str, need: list[str]) -> str:
    items = "、".join(need) if need else "主要财务数据"
    return f"{company}{label}中，{items}分别是多少？"


# 前置步骤的生成只为记笔记：一张表，数字带 [编号]，不写分析（越短越快，表格才能逐格核对）
PRESTEP_FOCUS = ("【这一步只为给后面的对比准备数据】只输出一张 Markdown 表格（项目、数值、来源 [编号]），"
                 "不要写结论、分析和要点；片段里找不到的项目整行不写。\n\n")


def read_step(sdoc: str, src: str, question: str, need: list[str], *, fin, search, gen,
              check) -> list[dict]:
    """一个前置步骤的核心：读一份报告、记笔记。app.py 和端到端评测共用。

    fin 是这份报告的主要会计数据；需要的数据超出它时，search(question) 检索、gen(question, hits) 只出表格，
    check(answer, hits) 逐格核对（rag/fidelity.unverified_in_tables），核对过的格子记成笔记。
    """
    got = from_financials(fin, sdoc, src)
    if not covered_by_financials(need):
        hits = search(question)
        answer = gen(question, hits)
        bad = (check(answer, hits) or {}).get("unverified") or []
        got = merge(got, from_answer(answer, hits, bad, sdoc, src))
    return got


def already_noted(history: list[dict], sdoc: str, need: list[str], turns: int = NOTE_TURNS) -> bool:
    """前几轮是否已经有这份报告**这次要的**数据，可以跳过前置步骤。

    前几轮记过这份报告的笔记，不等于记过这次要的数：之前问营收记下的是主要会计数据，这次问毛利率要的是营业成本
    （线上实测：跳过后主回答说「上半年营业成本未披露」）。所以只有要的数据都在主要会计数据范围内时才算已有。
    """
    if not covered_by_financials(need):
        return False
    return any(n.get("doc") == sdoc for e in history[-turns:] for n in e.get("notes") or [])
