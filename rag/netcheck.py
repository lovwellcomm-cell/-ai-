"""部署环境的出网自检：验证 Streamlit Cloud 能否访问巨潮资讯。

只在 URL 带 `?netcheck=1` 时由 app.py 渲染，不参与正常业务流程。
两个只读请求：
  · 公司搜索（POST）
  · 年报 PDF 的前 1KB（Range 请求，不下载整份）

结果只用于判断“能不能在国内站点下载年报”，不落盘、不改会话状态。
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
PDF_URL = "https://static.cninfo.com.cn/finalpage/2026-03-31/1225057751.PDF"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def _timed(fn) -> dict:
    t0 = time.perf_counter()
    try:
        with fn() as resp:
            body = resp.read(2048)
            return {
                "ok": 200 <= getattr(resp, "status", 200) < 300,
                "status": getattr(resp, "status", None),
                "ms": round((time.perf_counter() - t0) * 1000),
                "content_type": (resp.headers.get("Content-Type", "") if resp.headers else ""),
                "sample": body[:120].decode("utf-8", "replace") if body else "",
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "status": e.code,
            "ms": round((time.perf_counter() - t0) * 1000),
            "error": f"HTTP {e.code}: {e.read()[:120].decode('utf-8', 'replace')}",
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "ms": round((time.perf_counter() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}"[:200],
        }


def probe(timeout_search: float = 10.0, timeout_pdf: float = 15.0) -> dict:
    """返回 {'search': {...}, 'pdf': {...}}；每个请求独立计时，互不影响。"""
    form = urllib.parse.urlencode({"keyWord": "600577", "maxNum": "10"}).encode("utf-8")
    search_req = urllib.request.Request(
        SEARCH_URL, data=form, method="POST",
        headers={"User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    pdf_req = urllib.request.Request(
        PDF_URL, method="GET",
        headers={"User-Agent": UA, "Range": "bytes=0-1023"},
    )
    return {
        "search": _timed(lambda: _open(search_req, timeout_search)),
        "pdf": _timed(lambda: _open(pdf_req, timeout_pdf)),
    }
