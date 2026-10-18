"""出网探针的测试。全部 mock，不发真实请求。"""
import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import netcheck  # noqa: E402


class FakeResp:
    def __init__(self, status=200, body=b"[]", ctype="application/json"):
        self.status = status
        self.body = body
        self.headers = {"Content-Type": ctype}

    def read(self, n=-1):
        return self.body[:n] if n >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_reports_both_requests_and_headers(monkeypatch):
    seen = []

    def fake_open(req, timeout):
        seen.append(req)
        if req.full_url == netcheck.SEARCH_URL:
            return FakeResp(body=b'[{"code":"600577","zwjc":"jingda"}]')
        return FakeResp(status=206, body=b"%PDF-1.7", ctype="application/pdf")

    monkeypatch.setattr(netcheck, "_open", fake_open)
    out = netcheck.probe()

    assert out["search"]["ok"] is True and out["search"]["status"] == 200
    assert "600577" in out["search"]["sample"]
    assert out["pdf"]["ok"] is True and out["pdf"]["status"] == 206
    assert out["pdf"]["content_type"] == "application/pdf"
    assert out["pdf"]["sample"].startswith("%PDF")

    search_req = seen[0]
    assert search_req.get_method() == "POST"
    assert b"keyWord=600577" in search_req.data
    pdf_req = seen[1]
    assert pdf_req.get_header("Range") == "bytes=0-1023"


def test_probe_survives_http_error(monkeypatch):
    def fake_open(req, timeout):
        if req.full_url == netcheck.SEARCH_URL:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b"denied"))
        return FakeResp(status=206, body=b"%PDF")

    monkeypatch.setattr(netcheck, "_open", fake_open)
    out = netcheck.probe()

    assert out["search"]["ok"] is False and out["search"]["status"] == 403
    assert "403" in out["search"]["error"]
    # 一个请求失败不影响另一个
    assert out["pdf"]["ok"] is True


def test_probe_survives_timeout(monkeypatch):
    def fake_open(req, timeout):
        if req.full_url == netcheck.SEARCH_URL:
            raise TimeoutError("timed out")
        return FakeResp(status=206, body=b"%PDF")

    monkeypatch.setattr(netcheck, "_open", fake_open)
    out = netcheck.probe()

    assert out["search"]["ok"] is False and out["search"]["status"] is None
    assert "TimeoutError" in out["search"]["error"]
    assert out["pdf"]["ms"] >= 0
