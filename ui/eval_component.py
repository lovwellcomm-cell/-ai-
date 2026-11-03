"""注册并挂载评测页组件（web/eval.*）。数据是仓库里的快照 docs/eval/snapshot.json。

和 ui/landing_component.py 同一套做法：按文件 mtime 判断要不要重新注册，本地改样式不用重启。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SNAPSHOT = ROOT / "docs" / "eval" / "snapshot.json"
FILES = ("eval.html", "eval.css", "eval.js")


def _read(name: str) -> str:
    text = (WEB / name).read_text(encoding="utf-8")
    if "\n" not in text:
        raise ValueError(f"web/{name} 必须是多行文本，否则 Streamlit 会把它当成文件路径")
    return text


def _fingerprint() -> str:
    return "|".join(str((WEB / f).stat().st_mtime_ns) for f in FILES)


_cache: tuple[str, object] | None = None


def _renderer():
    global _cache
    stamp = _fingerprint()
    if _cache is None or _cache[0] != stamp:
        _cache = (stamp, st.components.v2.component(
            "annual_report_eval",
            html=_read("eval.html"),
            css=_read("eval.css"),
            js=_read("eval.js"),
        ))
    return _cache[1]


def load_snapshot() -> dict:
    """评测页的数据。快照还没生成或读不了时返回空 dict，页面写「评测数据还没有生成」。"""
    try:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def eval_surface(data: dict, *, key: str = "eval", on_go: Callable[[], None] | None = None):
    """挂载评测页，返回 ComponentResult；result.go 是顶栏点的去处（home / chat）。"""
    return _renderer()(
        key=key,
        data=data,
        width="stretch",
        height="content",
        on_go_change=on_go or (lambda: None),
    )
