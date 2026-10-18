"""巨潮链接下载器的测试。全部 mock，不发真实请求。"""
import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import disclosure  # noqa: E402

PDF_URL = "https://static.cninfo.com.cn/finalpage/2026-03-31/1225057751.PDF"


class FakeResp:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self, n=-1):
        if not self._body:
            return b""
        if n < 0:
            out, self._body = self._body, b""
            return out
        out, self._body = self._body[:n], self._body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def open(self, req, timeout=None):
        if self.exc:
            raise self.exc
        return self.resp


def use(monkeypatch, resp=None, exc=None):
    monkeypatch.setattr(disclosure, "_opener", lambda: FakeOpener(resp, exc))


# ---------------------------------------------------------------- 白名单

@pytest.mark.parametrize("url", [
    PDF_URL,
    "https://www.cninfo.com.cn/new/index",
    "https://static.cninfo.com.cn/x.pdf",
])
def test_allowed_hosts(url):
    assert disclosure.is_allowed_url(url) is True


@pytest.mark.parametrize("url", [
    "http://static.cninfo.com.cn/x.pdf",                 # 非 https
    "https://evil.com/x.pdf",                            # 非白名单
    "https://static.cninfo.com.cn.evil.com/x.pdf",       # 后缀伪装
    "https://user:pass@static.cninfo.com.cn/x.pdf",      # 带凭据
    "https://static.cninfo.com.cn:8443/x.pdf",           # 非 443 端口
    "file:///etc/passwd",
    "",
])
def test_rejected_urls(url):
    assert disclosure.is_allowed_url(url) is False


def test_redirect_to_other_host_is_rejected():
    h = disclosure._SafeRedirect()
    req = urllib.request.Request(PDF_URL)
    with pytest.raises(urllib.error.URLError):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.com/x.pdf")


# ---------------------------------------------------------------- 文件名

def test_filename_from_content_disposition():
    h = {"Content-Disposition": 'attachment; filename="jingda.PDF"'}
    assert disclosure._filename(h, PDF_URL) == "jingda.PDF"


def test_filename_from_rfc5987():
    h = {"Content-Disposition": "attachment; filename*=UTF-8''%E7%B2%BE%E8%BE%BE%E8%82%A1%E4%BB%BD.pdf"}
    assert disclosure._filename(h, PDF_URL) == "精达股份.pdf"


def test_filename_falls_back_to_url_and_adds_suffix():
    assert disclosure._filename({}, PDF_URL) == "1225057751.PDF"
    assert disclosure._filename({}, "https://static.cninfo.com.cn/download") == "download.pdf"


def test_filename_sanitizes_separators():
    h = {"Content-Disposition": 'attachment; filename="a/b:c.pdf"'}
    assert "/" not in disclosure._filename(h, PDF_URL)
    assert ":" not in disclosure._filename(h, PDF_URL)


# ---------------------------------------------------------------- 下载

def test_download_success(monkeypatch):
    body = b"%PDF-1.7\nhello"
    use(monkeypatch, resp=FakeResp(body, {
        "Content-Type": "application/pdf", "Content-Length": str(len(body)),
        "Content-Disposition": 'attachment; filename="jingda.pdf"'}))
    data, name = disclosure.download_pdf(PDF_URL)
    assert data == body and name == "jingda.pdf"


def test_html_page_is_rejected_with_a_helpful_message(monkeypatch):
    use(monkeypatch, resp=FakeResp(b"<html>not found</html>",
                                   {"Content-Type": "text/html; charset=utf-8"}))
    with pytest.raises(RuntimeError, match="网页"):
        disclosure.download_pdf(PDF_URL)


def test_non_pdf_content_is_rejected(monkeypatch):
    use(monkeypatch, resp=FakeResp(b"\x00\x01\x02", {"Content-Type": "application/octet-stream"}))
    with pytest.raises(RuntimeError, match="不是 PDF"):
        disclosure.download_pdf(PDF_URL)


def test_size_cap_during_stream(monkeypatch):
    use(monkeypatch, resp=FakeResp(b"%PDF" + b"x" * 200, {"Content-Type": "application/pdf"}))
    with pytest.raises(ValueError, match="上限"):
        disclosure.download_pdf(PDF_URL, max_bytes=100)


def test_size_cap_from_content_length(monkeypatch):
    use(monkeypatch, resp=FakeResp(b"%PDF", {
        "Content-Type": "application/pdf", "Content-Length": "999999"}))
    with pytest.raises(ValueError, match="上限"):
        disclosure.download_pdf(PDF_URL, max_bytes=1000)


def test_http_error_becomes_runtime_error(monkeypatch):
    err = urllib.error.HTTPError(PDF_URL, 403, "Forbidden", {}, io.BytesIO(b"denied"))
    use(monkeypatch, exc=err)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        disclosure.download_pdf(PDF_URL)


def test_network_error_becomes_runtime_error(monkeypatch):
    use(monkeypatch, exc=urllib.error.URLError("connection reset"))
    with pytest.raises(RuntimeError, match="下载失败"):
        disclosure.download_pdf(PDF_URL)


def test_non_allowlisted_url_fails_before_any_request(monkeypatch):
    def boom():
        raise AssertionError("不应该发起请求")

    monkeypatch.setattr(disclosure, "_opener", boom)
    with pytest.raises(ValueError):
        disclosure.download_pdf("https://evil.com/x.pdf")


# ---------------------------------------------------------------- 来源标记

def test_source_site_label():
    assert disclosure.source_site(PDF_URL) == "巨潮资讯"
    assert disclosure.source_site("https://example.com/x.pdf") == ""


# ---------------------------------------------------------------- 巨潮检索

COMPANY = [{"code": "002029", "orgId": "gssz0002029", "zwjc": "七匹狼"}]


def mock_posts(monkeypatch, responses):
    calls = []

    def fake(url, form, timeout):
        calls.append((url, form))
        return responses.pop(0)

    monkeypatch.setattr(disclosure, "_post_form", fake)
    return calls


def test_search_company_parses_topsearch(monkeypatch):
    mock_posts(monkeypatch, [COMPANY])
    out = disclosure.search_company("七匹狼")
    assert out == [{"code": "002029", "org_id": "gssz0002029", "name": "七匹狼"}]


def test_pick_company_prefers_exact_short_name():
    results = [{"code": "600519", "org_id": "x", "name": "贵州茅台"},
               {"code": "002029", "org_id": "y", "name": "七匹狼"}]
    assert disclosure.pick_company(results, "七匹狼")["code"] == "002029"
    assert disclosure.pick_company(results, "七匹狼股份")["code"] == "002029"
    assert disclosure.pick_company([], "七匹狼") is None


def test_list_annual_reports_filters_summaries_and_builds_url(monkeypatch):
    payload = {"announcements": [
        {"announcementTitle": "七匹狼 2025 年年度报告",
         "adjunctUrl": "finalpage/2026-04-03/1225077462.PDF",
         "adjunctSize": 3892, "announcementTime": 1},
        {"announcementTitle": "七匹狼2025年年度报告摘要",
         "adjunctUrl": "finalpage/2026-04-03/1225077461.PDF", "adjunctSize": 392},
        {"announcementTitle": "七匹狼2025年年度报告（英文版）",
         "adjunctUrl": "finalpage/2026-04-03/1225077463.PDF", "adjunctSize": 1602},
    ]}
    calls = mock_posts(monkeypatch, [payload])
    out = disclosure.list_annual_reports("002029", "gssz0002029", 2025)
    assert len(out) == 1
    assert out[0]["url"] == ("https://static.cninfo.com.cn/finalpage/2026-04-03/"
                             "1225077462.PDF")
    assert out[0]["size_kb"] == 3892
    form = calls[0][1]
    assert form["seDate"] == "2026-01-01~2026-12-31"   # 年报在次年披露
    assert form["stock"] == "002029,gssz0002029"
    assert form["column"] == "szse"
    assert form["category"] == "category_ndbg_szsh"


def test_find_report_walks_back_to_the_latest_available_year(monkeypatch):
    monkeypatch.setattr(disclosure, "_current_year", lambda: 2026)
    report = {"announcementTitle": "七匹狼2024年年度报告",
              "adjunctUrl": "finalpage/2025-04-03/1.PDF", "adjunctSize": 4153,
              "announcementTime": 2}
    calls = mock_posts(monkeypatch, [COMPANY, {"announcements": []},
                                     {"announcements": [report]}])
    out = disclosure.find_report("七匹狼")
    assert out["year"] == 2024 and out["company"] == "七匹狼"
    assert len(calls) == 3                       # 搜索 + 2025 空 + 2024 命中


def test_find_report_uses_the_requested_year(monkeypatch):
    report = {"announcementTitle": "七匹狼2024年年度报告",
              "adjunctUrl": "finalpage/2025-04-03/1.PDF", "adjunctSize": 4153}
    calls = mock_posts(monkeypatch, [COMPANY, {"announcements": [report]}])
    out = disclosure.find_report("七匹狼", 2024)
    assert out["year"] == 2024
    assert calls[1][1]["seDate"] == "2025-01-01~2025-12-31"


def test_find_report_raises_when_company_is_unknown(monkeypatch):
    mock_posts(monkeypatch, [[]])
    with pytest.raises(disclosure.ReportNotFound):
        disclosure.find_report("不存在的公司")


def test_ensure_report_downloads_the_selected_pdf(monkeypatch):
    monkeypatch.setattr(disclosure, "find_report",
                        lambda company, year=None: {"url": PDF_URL, "year": 2025,
                                                    "company": company})
    monkeypatch.setattr(disclosure, "download_pdf", lambda url: (b"%PDF-1.7", "x.pdf"))
    data, name, info = disclosure.ensure_report("七匹狼", 2025)
    assert data.startswith(b"%PDF") and name == "x.pdf" and info["year"] == 2025
