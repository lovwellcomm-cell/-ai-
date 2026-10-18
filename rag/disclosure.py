"""从巨潮资讯下载年报 PDF。

第 1 阶段只做“粘贴链接 → 下载 → 复用现有 ingest”，不做公司检索。
安全边界：
  · 只接受 https、只接受白名单域名，禁止用户名密码、禁止非 443 端口；
  · 重定向目标也必须过同一套白名单（防 SSRF）；
  · 限制 40 MB、30 秒超时，内容必须以 %PDF 开头；
  · 不落盘，直接把 bytes 交给 ingest。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from . import config
from .company import normalize

ALLOWED_HOSTS = ("www.cninfo.com.cn", "static.cninfo.com.cn")
QUERY_PAGE_HOST = "www.cninfo.com.cn"
PDF_HOST = "static.cninfo.com.cn"
TOPSEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def source_site(url: str) -> str:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return "巨潮资讯" if host.endswith("cninfo.com.cn") else ""


def is_allowed_url(url: str) -> bool:
    """只放行巨潮的 https 地址；其他一律拒绝。"""
    try:
        p = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return False
    if p.scheme != "https" or p.username or p.password:
        return False
    if p.port not in (None, 443):
        return False
    return (p.hostname or "").lower() in ALLOWED_HOSTS


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """跟随重定向，但新地址也必须过白名单。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not is_allowed_url(target):
            raise urllib.error.URLError(f"拒绝跳转到非白名单地址：{target}")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _opener():
    return urllib.request.build_opener(_SafeRedirect())


def _filename(headers, url: str) -> str:
    """优先 Content-Disposition，其次 URL 最后一段，兜底 annual-report.pdf。"""
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if not m:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    name = urllib.parse.unquote(m.group(1).strip()) if m else ""
    if not name:
        name = urllib.parse.unquote(url.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
    name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", name).strip()
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:120] or "annual-report.pdf"


def download_pdf(url: str, max_bytes: int | None = None,
                 timeout: float | None = None) -> tuple[bytes, str]:
    """下载并校验 PDF，返回 (bytes, 文件名)。失败抛 ValueError / RuntimeError。"""
    url = (url or "").strip()
    if not is_allowed_url(url):
        raise ValueError("只支持巨潮资讯的 https PDF 链接（www.cninfo.com.cn / "
                         "static.cninfo.com.cn）。")
    max_bytes = max_bytes or config.REPORT_DOWNLOAD_MAX_MB * 1024 * 1024
    timeout = timeout or config.REPORT_DOWNLOAD_TIMEOUT

    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    })
    opener = _opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)} MB 上限。")
            chunks: list[bytes] = []
            total = 0
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                total += len(buf)
                if total > max_bytes:
                    raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)} MB 上限。")
                chunks.append(buf)
            data = b"".join(chunks)
            headers = resp.headers
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"下载失败：HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"下载失败：{e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError("下载超时，请检查链接或稍后重试。") from e

    if not data:
        raise RuntimeError("下载内容为空。")
    if not data.startswith(b"%PDF"):
        if "html" in ctype:
            raise RuntimeError("这个链接返回的是网页，不是 PDF。请打开年报页面，"
                               "复制 PDF 文件本身的地址。")
        raise RuntimeError("下载内容不是 PDF。")
    return data, _filename(headers, url)


# ---------------------------------------------------------------- 巨潮检索

class ReportNotFound(RuntimeError):
    """公司或年报查不到。调用方应明确告知用户，而不是拿当前文档硬答。"""


def _post_form(url: str, form: dict, timeout: float) -> dict | list:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"巨潮接口 HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"巨潮接口连接失败：{e.reason}") from e


def _current_year() -> int:
    """单独抽出来，测试里可以固定年份。"""
    return date.today().year


def search_company(keyword: str) -> list[dict]:
    """公司名/代码/拼音 → [{code, org_id, name}]。中文必须 UTF-8 编码。"""
    keyword = (keyword or "").strip()[:32]
    if not keyword:
        return []
    data = _post_form(TOPSEARCH_URL, {"keyWord": keyword, "maxNum": 10},
                      config.REPORT_SEARCH_TIMEOUT)
    out = []
    for x in data or []:
        code = str(x.get("code") or "")
        org_id = str(x.get("orgId") or "")
        name = str(x.get("zwjc") or "")
        if code and org_id and name:
            out.append({"code": code, "org_id": org_id, "name": name})
    return out


def pick_company(results: list[dict], keyword: str) -> dict | None:
    """优先精确匹配简称，其次包含匹配，最后取第一条。"""
    if not results:
        return None
    k = normalize(keyword)
    for r in results:
        if normalize(r["name"]) == k:
            return r
    for r in results:
        n = normalize(r["name"])
        if k and (k in n or n in k):
            return r
    return results[0]


def list_annual_reports(code: str, org_id: str, year: int) -> list[dict]:
    """查某公司某报告年度的年报公告（披露日期在次年）。"""
    column = "sse" if str(code).startswith("6") else "szse"
    form = {
        "pageNum": 1, "pageSize": 30, "column": column, "tabName": "fulltext",
        "plate": "", "stock": f"{code},{org_id}", "searchkey": "", "secid": "",
        "category": "category_ndbg_szsh", "trade": "",
        "seDate": f"{year + 1}-01-01~{year + 1}-12-31",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    data = _post_form(ANNOUNCEMENT_URL, form, config.REPORT_SEARCH_TIMEOUT) or {}
    want = f"{year}年年度报告"
    out = []
    for a in data.get("announcements") or []:
        title = str(a.get("announcementTitle") or "").strip()
        compact = re.sub(r"\s+", "", title)
        if want not in compact:
            continue
        if any(x in compact for x in ("摘要", "英文", "已取消")):
            continue
        url = "https://static.cninfo.com.cn/" + str(a.get("adjunctUrl") or "")
        if not is_allowed_url(url):
            continue
        try:
            size = int(a.get("adjunctSize") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"title": title, "url": url, "year": year, "size_kb": size,
                    "date": str(a.get("announcementTime") or "")})
    return out


def find_report(company: str, year: int | None = None) -> dict:
    """公司名 + 年份 → 年报公告。年份为空时从最近三年往前找。"""
    picked = pick_company(search_company(company), company)
    if not picked:
        raise ReportNotFound(f"巨潮没有找到名为“{company}”的上市公司")
    years = [int(year)] if year else [_current_year() - 1, _current_year() - 2,
                                      _current_year() - 3]
    for y in years:
        reports = list_annual_reports(picked["code"], picked["org_id"], y)
        if reports:
            reports.sort(key=lambda r: r.get("date") or "", reverse=True)
            best = dict(reports[0])
            best.update({"company": picked["name"], "code": picked["code"]})
            return best
    raise ReportNotFound(f"没有找到“{company}” {year or '最近三年'}的年报公告")


def ensure_report(company: str, year: int | None = None) -> tuple[bytes, str, dict]:
    """找公告 → 下载 PDF。返回 (bytes, 文件名, 公告信息)。"""
    info = find_report(company, year)
    data, filename = download_pdf(info["url"])
    return data, filename, info
