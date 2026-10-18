"""部署守卫：源码戳与模块卸载。

这里**不**跑 `ensure_fresh()` 的默认路径 —— 那会把测试进程自己的 rag 模块卸掉。
所有会触发卸载的用例都先快照 sys.modules，结束后原样放回。
"""
import os
import sys
import types

import pytest

from rag import freshness

KEYS = ("ARB_SOURCE_STAMP", "ARB_PREV_STAMP", "ARB_RELOAD_AT",
        "ARB_RELOAD_COUNT", "ARB_PROCESS_STARTED", "ARB_CACHE_BUST")


@pytest.fixture(autouse=True)
def clean_env():
    """ensure_fresh 直接写 os.environ，monkeypatch 管不到，自己收尾。"""
    saved = {k: os.environ.pop(k) for k in KEYS if k in os.environ}
    yield
    for key in KEYS:
        os.environ.pop(key, None)
    os.environ.update(saved)


@pytest.fixture
def keep_modules():
    """快照 rag / ui 的模块表，卸载用例结束后恢复。"""
    saved = {n: m for n, m in list(sys.modules.items())
             if n.split(".", 1)[0] in ("rag", "ui")}
    yield saved
    sys.modules.update(saved)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_source_stamp_is_content_sensitive(tmp_path):
    a = _write(tmp_path, "a.py", "x = 1\n")
    b = _write(tmp_path, "b.py", "y = 2\n")
    stamp = freshness.source_stamp([a, b])
    assert len(stamp) == 12
    assert freshness.source_stamp([a, b]) == stamp
    b.write_text("y = 22\n", encoding="utf-8")
    assert freshness.source_stamp([a, b]) != stamp


def test_source_stamp_ignores_mtime(tmp_path):
    a = _write(tmp_path, "a.py", "x = 1\n")
    stamp = freshness.source_stamp([a])
    os.utime(a, (1_000_000_000, 1_000_000_000))   # 只动时间戳，不动内容
    assert freshness.source_stamp([a]) == stamp


def test_source_stamp_gives_up_on_unreadable_file(tmp_path):
    a = _write(tmp_path, "a.py", "x = 1\n")
    assert freshness.source_stamp([a, tmp_path / "missing.py"]) == ""


def test_evict_removes_whole_packages(keep_modules):
    fakes = [types.ModuleType(n) for n in ("rag.fake", "ui.fake", "ragfoo")]
    try:
        for mod in fakes:
            sys.modules[mod.__name__] = mod
        doomed = freshness._evict()
        assert {"rag", "rag.freshness", "rag.fake", "ui.fake"} <= set(doomed)
        assert "ragfoo" not in doomed
        assert "rag.fake" not in sys.modules
        assert "ragfoo" in sys.modules          # 前缀相同但不是本包，不能误伤
    finally:
        for mod in fakes:
            sys.modules.pop(mod.__name__, None)


def test_first_call_records_without_evicting(tmp_path):
    a = _write(tmp_path, "a.py", "x = 1\n")
    assert freshness.ensure_fresh([a]) == freshness.source_stamp([a])
    assert "rag" in sys.modules                # 进程刚启动，没有旧模块可卸
    assert "ARB_RELOAD_COUNT" not in os.environ


def test_same_stamp_is_a_noop(tmp_path):
    a = _write(tmp_path, "a.py", "x = 1\n")
    freshness.ensure_fresh([a])
    before = dict(sys.modules)
    freshness.ensure_fresh([a])
    assert sys.modules == before
    assert "ARB_RELOAD_AT" not in os.environ


def test_changed_stamp_evicts_and_marks_cache_bust(tmp_path, keep_modules):
    a = _write(tmp_path, "a.py", "x = 1\n")
    freshness.ensure_fresh([a])
    a.write_text("x = 2222\n", encoding="utf-8")

    stamp = freshness.ensure_fresh([a])
    assert stamp != os.environ["ARB_PREV_STAMP"]
    assert os.environ["ARB_RELOAD_COUNT"] == "1"
    assert "rag.freshness" not in sys.modules
    assert "ARB_RELOAD_AT" in os.environ
    assert freshness.take_cache_bust(stamp) is True
    assert freshness.take_cache_bust(stamp) is False


def test_build_report_shape():
    report = freshness.build_report("deadbeef0000")
    assert report["source_stamp"] == "deadbeef0000"
    assert {"stamps_match", "reload_count", "process_started",
            "verdict", "tracked_files"} <= set(report)
