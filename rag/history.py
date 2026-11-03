"""多年趋势：再抽一份往年年报的「主要会计数据」表，把指标卡的近三年拉长到六年。

为什么只抽表、不建索引：趋势只需要那几行数字，建索引要嵌入几百个片段、几十秒，
还会占掉访客「每会话导入 2 份」的额度。往年年报读前 30 页、抽完表 PDF 就扔，只留几 KB 的 JSON。

为什么是「往前数第三年」的那份：2025 年报的表里是 2025/2024/2023，2022 年报的表里是
2022/2021/2020 —— 两份正好接成连续六年。每张表各自已经过「增减复算」（rag/financials.py:verify），
没通过的科目不进序列；某个科目只要缺一年就不画长图，免得折线中间断一截还装作连续。

来源顺序：本地已有的同公司年报（比如之前导入过）→ data/_history 缓存 → 巨潮下载。
缓存按公司 + 年份存，全站共用：第一个访客等几秒，之后的人直接用。

第二步（往年数字进回答）：往年那张表也做成一个笔记式片段（as_hit），接在检索片段后面编号，
回答能引用、逐格核对、记成笔记。巨潮下载的那份不留 PDF，所以扔之前把表所在的那一页渲染成 PNG 存下，
片段的 doc_id 写成「hist:<缓存键>」，翻原页时从这张图出（page_png）。本地已有的年报直接用它自己的 doc_id。
"""
from __future__ import annotations

import json
import re
import time

from . import config
from .company import normalize, same_company
from .financials import FIN_VERSION, extract, load_or_build
from .periods import FY, report_label

HIST_DIR_NAME = "_history"
HIST_PREFIX = "hist:"
HIST_SECTION = "往年年报"                 # 片段标题；rag/generator.NOTES_RULE 按这个名字讲怎么用
# 「这份年报巨潮上没有」（比如公司 2023 年才上市）也缓存，但一周后再查一次，
# 万一当时只是检索接口抽风。网络错误不缓存。
MISS_TTL = 7 * 24 * 3600
_HIST_ID_RE = re.compile(r"^hist:(\w{1,40}_\d{4})$")

_TREND_RE = re.compile(
    r"趋势|走势|历年|多年|这几年|近几年|过去几年|长期|"
    r"(近|过去|最近)\s*[三四五六七八九十3-9]\s*(个)?\s*(年|个?财年)"
)
_FOLLOWUP_RE = re.compile(r"^(那|那么)?.{0,8}(呢|如何|怎样|怎么样)[？?]?$")


def wants_trend(question: str, previous: list[str] | tuple[str, ...] = ()) -> bool:
    """这一问要不要把卡片拉长到六年。短追问（「那净利润呢」）跟着上一问走。"""
    q = (question or "").strip()
    if previous and _FOLLOWUP_RE.match(q) and not _TREND_RE.search(q):
        *earlier, last = previous
        return wants_trend(last, earlier)
    return bool(_TREND_RE.search(q))


def older_year(fin: dict) -> int | None:
    """往年那份年报的年度：当前表里最早一年再往前一年。"""
    try:
        return int(fin["years"][-1]) - 1
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _cache_key(company: str, year: int, period: str = FY) -> str:
    """年报「精达_2022」；季报 / 半年报（rag/quarters.py 拼单季序列用）「精达_2025_Q3」。"""
    key = re.sub(r"[^\w一-鿿]", "_", normalize(company))[:40] or "_"
    return f"{key}_{year}" if period == FY else f"{key}_{year}_{period}"


def _cache_path(company: str, year: int, period: str = FY):
    return config.DATA_DIR / HIST_DIR_NAME / f"{_cache_key(company, year, period)}.json"


def _page_path(key: str, page: int):
    return config.DATA_DIR / HIST_DIR_NAME / f"{key}_p{int(page)}.png"


def is_hist_id(doc_id) -> bool:
    return isinstance(doc_id, str) and bool(_HIST_ID_RE.match(doc_id))


def page_png(doc_id: str, page: int) -> bytes | None:
    """翻原页：往年年报表所在页的截图。doc_id 来自浏览器（可能被改过），只认格式对、文件在的。"""
    m = _HIST_ID_RE.match(doc_id or "")
    if not m:
        return None
    try:
        return _page_path(m.group(1), page).read_bytes()
    except (OSError, ValueError):
        return None


def _read_cache(company: str, year: int, period: str, now: float) -> tuple[bool, dict | None]:
    """返回 (命中与否, 结果)。命中但结果为 None = 确认过没有这份报告。"""
    try:
        cached = json.loads(_cache_path(company, year, period).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if cached.get("version") != FIN_VERSION:
        return False, None
    fin = cached.get("fin")
    if fin:
        # 第二步之前写的缓存没存表所在页的截图：算没命中（还能下载就重下一次补上），数字留着兜底
        return "shot" in cached, fin
    if now - float(cached.get("at") or 0) < MISS_TTL:
        return True, None
    return False, None


def _write_cache(company: str, year: int, period: str, fin: dict | None, now: float) -> None:
    path = _cache_path(company, year, period)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": FIN_VERSION, "company": company, "year": year,
                                    "at": now, "fin": fin, "shot": True},
                                   ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError:
        pass


def _from_local(company: str, year: int, period: str, docs: list[dict]) -> dict | None:
    """本地已经有这份报告（之前导入过、或示例文档）就直接读它的表。"""
    for d in docs:
        if (d.get("period") or FY) != period or str(d.get("year") or "") != str(year):
            continue
        if not same_company(d.get("company") or "", company):
            continue
        fin = load_or_build(d["doc_id"])
        if fin and fin.get("years") and fin["years"][0] == str(year):
            return dict(fin, source="本地", doc_id=d["doc_id"])
    return None


def _save_page(company: str, year: int, data: bytes, page: int) -> None:
    from .pdf_parser import render_page_png

    path = _page_path(_cache_key(company, year), page)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_page_png(data, page))
    except Exception:
        pass                                   # 截图失败只是翻不了原页，数字照用


def _from_cninfo(company: str, year: int, period: str = FY) -> dict | None:
    """巨潮找到那份报告 → 下载 → 只读前 30 页抽表；年报另存表所在页的截图（季报只画单季折线，不进回答）。
    找不到公告返回 None，网络错误往外抛。"""
    from .disclosure import ReportNotFound, download_pdf, find_report

    try:
        info = find_report(company, year, period)
    except ReportNotFound:
        return None
    data, _ = download_pdf(info["url"], timeout=config.HISTORY_DOWNLOAD_TIMEOUT)
    fin = extract(data, period=period, year=year)
    if not fin or fin.get("years", [None])[0] != str(year):
        # 表头年份对不上（抽到了别的表、或者公告标题和正文年份不一致）就当没抽到
        return None
    if period == FY:
        _save_page(company, year, data, fin["page"])
    return dict(fin, source=info["url"])


def load_report(company: str, year: int, period: str, docs: list[dict], *, allow_fetch: bool = True,
                now: float | None = None) -> tuple[dict | None, bool]:
    """取 company 在 year 年度 period 那份报告里的主要会计数据。

    返回 (fin 或 None, 是否真的从巨潮下载了)。调用方用第二项计会话的下载次数。
    allow_fetch=False 时只看本地和缓存（会话下载次数用完了）。
    """
    now = time.time() if now is None else now
    local = _from_local(company, year, period, docs)
    if local:
        return local, False
    hit, fin = _read_cache(company, year, period, now)
    if hit or not allow_fetch:
        return fin, False
    fin = _from_cninfo(company, year, period)  # 网络错误直接抛给调用方，不写缓存
    _write_cache(company, year, period, fin, now)
    return fin, True


def load_older(company: str, year: int, docs: list[dict], *, allow_fetch: bool = True,
               now: float | None = None) -> tuple[dict | None, bool]:
    """往年那份年报（拉长到六年用）。"""
    return load_report(company, year, FY, docs, allow_fetch=allow_fetch, now=now)


def _span(years: list[str]) -> str:
    return f"{years[-1]}–{years[0]}" if len(years) > 1 else years[0]


def merge(fin: dict, older: dict | None) -> dict:
    """把往年那份表接到当前表后面。返回新 dict（不改入参），没有可接的就原样返回。

    years 按新 → 旧排列，和 fin["years"] 的约定一致。科目两份都有才合并；
    重叠的年份以新报告为准（新报告里的旧年数字可能追溯调整过）。
    """
    if not older or not older.get("items") or (fin.get("period") or FY) != FY:
        return fin
    new_years = list(fin["years"])
    old_years = [y for y in older["years"] if y not in new_years]
    if not old_years or int(old_years[0]) != int(new_years[-1]) - 1:
        return fin                                   # 接不上（中间缺一年）就不接
    items = {}
    for key, item in fin["items"].items():
        values = dict(item["values"])
        prev = older["items"].get(key)
        if prev:
            for y in old_years:
                if y in prev["values"]:
                    values.setdefault(y, prev["values"][y])
        items[key] = dict(item, values=values)
    out = dict(fin, items=items, years=new_years + old_years)
    out["sources"] = [
        {"years": _span(new_years), "label": f"{new_years[0]} 年报", "page": fin.get("page")},
        {"years": _span(old_years), "label": f"{older['years'][0]} 年报",
         "page": older.get("page")},
    ]
    return out


def as_hit(older: dict | None, company: str) -> dict | None:
    """往年那份表 → 一个笔记式片段（和 rag/notes.as_hits 同形状），接在检索片段后面编号。

    每个科目一行，写法和事实笔记一样（notes.from_financials），回答能引用、逐格核对、记成笔记；
    本地有的年报用它自己的 doc_id，巨潮下载的用「hist:<缓存键>」，翻原页走截图。
    """
    from .notes import from_financials

    if not older or not older.get("items") or not older.get("page"):
        return None
    year = older["years"][0]
    doc = older.get("doc_id") or HIST_PREFIX + _cache_key(company, int(year))
    src = f"{company} {report_label(year, FY)}".strip()
    lines = [n["t"] for n in from_financials(older, doc, src)]
    if not lines:
        return None
    return {"doc_id": doc, "note_src": src, "note": True, "text": "\n".join(lines),
            "section": f"{HIST_SECTION} · {src}", "subsection": "主要会计数据",
            "page_start": int(older["page"]), "page_end": int(older["page"])}


def attach(hits: list[dict], hit: dict | None) -> list[dict]:
    """把往年片段接到最后；前几轮记过同一页笔记的片段换成这张整表，免得同一页占两个编号。"""
    if not hit:
        return list(hits)
    key = (hit["doc_id"], hit["page_start"])
    return [h for h in hits if (h.get("doc_id"), h.get("page_start")) != key] + [hit]
