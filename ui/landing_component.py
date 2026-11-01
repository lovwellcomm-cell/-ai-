"""注册并挂载开屏页组件（web/landing.*）。

和 ui/chat_component.py 同一套做法：按文件 mtime 判断要不要重新注册，本地改样式不用重启。
产品截图 web/landing-shot.webp 在注册时转成 data URI 填进 html 的 {{SHOT}}——
组件拿不到 web/ 目录下的静态文件，内联是最简单的办法（截图约 30 KB）。
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Callable

import streamlit as st

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ("landing.html", "landing.css", "landing.js", "landing-shot.webp")


def _read(name: str) -> str:
    text = (WEB / name).read_text(encoding="utf-8")
    if "\n" not in text:
        raise ValueError(f"web/{name} 必须是多行文本，否则 Streamlit 会把它当成文件路径")
    return text


def _html() -> str:
    shot = base64.b64encode((WEB / "landing-shot.webp").read_bytes()).decode("ascii")
    return _read("landing.html").replace("{{SHOT}}", "data:image/webp;base64," + shot)


def _fingerprint() -> str:
    return "|".join(str((WEB / f).stat().st_mtime_ns) for f in FILES)


_cache: tuple[str, object] | None = None


def _renderer():
    global _cache
    stamp = _fingerprint()
    if _cache is None or _cache[0] != stamp:
        _cache = (stamp, st.components.v2.component(
            "annual_report_landing",
            html=_html(),
            css=_read("landing.css"),
            js=_read("landing.js"),
        ))
    return _cache[1]


def landing_surface(data: dict, *, key: str = "landing",
                    on_enter: Callable[[], None] | None = None):
    """挂载开屏页，返回 ComponentResult；result.enter 有值表示点了「开始聊天」。"""
    return _renderer()(
        key=key,
        data=data,
        width="stretch",
        height="content",
        on_enter_change=on_enter or (lambda: None),
    )
