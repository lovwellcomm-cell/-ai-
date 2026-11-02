"""部署环境的出网自检：验证 Streamlit Cloud 能否访问巨潮资讯，以及行情数据的官方来源。

只在 URL 带 `?netcheck=1` 时由 app.py 渲染，不参与正常业务流程。
巨潮两个只读请求：
  · 公司搜索（POST）
  · 年报 PDF 的前 1KB（Range 请求，不下载整份）
行情（「影响这家公司的几件大事」要用）：上期所、郑商所、广期所的日行情，
中国外汇交易中心的人民币中间价和 LPR。都是交易所 / 官方机构公开发布的数据。
大商所的公开页面有访问校验，不在这里（也不去绕开它）。

结果只用于判断“部署环境能不能拿到这些数据”，不落盘、不改会话状态。
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


# 行情来源：GET 的直接给地址，POST 的给 (地址, 表单)。日期固定成一个已知交易日，只测能不能连上。
# 广期所的 https 证书主机名不匹配（2026-11-02 本机实测），只能走 http；数据是公开行情，不带任何凭据。
MARKET = {
    "shfe": "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx20261030.dat",
    "czce": "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20261030/FutureDataDaily.txt",
    "gfex": ("http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList",
             {"trade_date": "20261030", "trade_type": "0"}),
    "fx_ccpr": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
    "lpr": "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/currency/bk-lpr.json",
}


def probe_market(timeout: float = 12.0) -> dict:
    """{来源: {...}}：每个行情来源一个只读请求，只读前 2KB。"""
    out = {}
    for name, target in MARKET.items():
        if isinstance(target, tuple):
            url, form = target
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode(form).encode("utf-8"), method="POST",
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        else:
            req = urllib.request.Request(target, method="GET", headers={"User-Agent": UA})
        out[name] = _timed(lambda r=req: _open(r, timeout))
    return out


def probe(timeout_search: float = 10.0, timeout_pdf: float = 15.0) -> dict:
    """返回 {'search': {...}, 'pdf': {...}, 'market': {...}}；每个请求独立计时，互不影响。"""
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
        "market": probe_market(),
    }
