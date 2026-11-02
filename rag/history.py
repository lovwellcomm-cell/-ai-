"""多年趋势：再抽一份往年年报的「主要会计数据」表，把指标卡的近三年拉长到六年。

为什么只抽表、不建索引：趋势只需要那几行数字，建索引要嵌入几百个片段、几十秒，
还会占掉访客「每会话导入 2 份」的额度。往年年报读前 30 页、抽完表 PDF 就扔，只留几 KB 的 JSON。

为什么是「往前数第三年」的那份：2025 年报的表里是 2025/2024/2023，2022 年报的表里是
2022/2021/2020 —— 两份正好接成连续六年。每张表各自已经过「增减复算」（rag/financials.py:verify），
没通过的科目不进序列；某个科目只要缺一年就不画长图，免得折线中间断一截还装作连续。

来源顺序：本地已有的同公司年报（比如之前导入过）→ data/_history 缓存 → 巨潮下载。
缓存按公司 + 年份存，全站共用：第一个访客等几秒，之后的人直接用。
"""
from __future__ import annotations

import json
import re
import time

from . import config
from .company import normalize, same_company
from .financials import FIN_VERSION, extract, load_or_build
from .periods import FY

HIST_DIR_NAME = "_history"
# 「这份年报巨潮上没有」（比如公司 2023 年才上市）也缓存，但一周后再查一次，
# 万一当时只是检索接口抽风。网络错误不缓存。
MISS_TTL = 7 * 24 * 3600

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


def _cache_path(company: str, year: int):
    key = re.sub(r"[^\w一-鿿]", "_", normalize(company))[:40] or "_"
    return config.DATA_DIR / HIST_DIR_NAME / f"{key}_{year}.json"


def _read_cache(company: str, year: int, now: float) -> tuple[bool, dict | None]:
    """返回 (命中与否, 结果)。命中但结果为 None = 确认过没有这份年报。"""
    try:
        cached = json.loads(_cache_path(company, year).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if cached.get("version") != FIN_VERSION:
        return False, None
    fin = cached.get("fin")
    if fin:
        return True, fin
    if now - float(cached.get("at") or 0) < MISS_TTL:
        return True, None
    return False, None


def _write_cache(company: str, year: int, fin: dict | None, now: float) -> None:
    path = _cache_path(company, year)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": FIN_VERSION, "company": company, "year": year,
                                    "at": now, "fin": fin}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError:
        pass


def _from_local(company: str, year: int, docs: list[dict]) -> dict | None:
    """本地已经有这份年报（之前导入过、或示例文档）就直接读它的表。"""
    for d in docs:
        if (d.get("period") or FY) != FY or str(d.get("year") or "") != str(year):
            continue
        if not same_company(d.get("company") or "", company):
            continue
        fin = load_or_build(d["doc_id"])
        if fin and fin.get("years") and fin["years"][0] == str(year):
            return dict(fin, source="本地")
    return None


def _from_cninfo(company: str, year: int) -> dict | None:
    """巨潮找到那份年报 → 下载 → 只读前 30 页抽表。找不到公告返回 None，网络错误往外抛。"""
    from .disclosure import ReportNotFound, download_pdf, find_report

    try:
        info = find_report(company, year, FY)
    except ReportNotFound:
        return None
    data, _ = download_pdf(info["url"], timeout=config.HISTORY_DOWNLOAD_TIMEOUT)
    fin = extract(data, period=FY)
    if not fin or fin.get("years", [None])[0] != str(year):
        # 表头年份对不上（抽到了别的表、或者公告标题和正文年份不一致）就当没抽到
        return None
    return dict(fin, source=info["url"])


def load_older(company: str, year: int, docs: list[dict], *, allow_fetch: bool = True,
               now: float | None = None) -> tuple[dict | None, bool]:
    """取 company 在 year 年度年报里的主要会计数据。

    返回 (fin 或 None, 是否真的从巨潮下载了)。调用方用第二项计会话的下载次数。
    allow_fetch=False 时只看本地和缓存（会话下载次数用完了）。
    """
    now = time.time() if now is None else now
    local = _from_local(company, year, docs)
    if local:
        return local, False
    hit, fin = _read_cache(company, year, now)
    if hit or not allow_fetch:
        return fin, False
    fin = _from_cninfo(company, year)          # 网络错误直接抛给调用方，不写缓存
    _write_cache(company, year, fin, now)
    return fin, True


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
