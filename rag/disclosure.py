"""从巨潮资讯下载年报 PDF。

第 1 阶段只做“粘贴链接 → 下载 → 复用现有 ingest”，不做公司检索。
安全边界：
  · 只接受 https、只接受白名单域名，禁止用户名密码、禁止非 443 端口；
  · 重定向目标也必须过同一套白名单（防 SSRF）；
  · 限制 40 MB、30 秒超时，内容必须以 %PDF 开头；
  · 不落盘，直接把 bytes 交给 ingest。
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request

from . import config

ALLOWED_HOSTS = ("www.cninfo.com.cn", "static.cninfo.com.cn")
QUERY_PAGE_HOST = "www.cninfo.com.cn"
PDF_HOST = "static.cninfo.com.cn"
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
