"""注册并挂载对话面板组件。

Streamlit 在**模块导入时**就把 html/css/js 的内容抓走了，而 web/ 下的文件不在它的文件监视
范围内（它只监视打包组件的 asset_dir）。所以改了 web/chat.css 之类，浏览器刷新是没用的。

这里按文件 mtime 判断内容有没有变，变了就重新注册一次。生产环境进程启动一次、
mtime 不会再变，等于只注册一次；本地改样式则省掉重启。重新注册同名组件时
Streamlit 会记一条 "already registered" 警告并以最后注册的为准，属于预期行为。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import streamlit as st

WEB = Path(__file__).resolve().parent.parent / "web"
FILES = ("chat.html", "chat.css", "chat.js")


def _read(name: str) -> str:
    text = (WEB / name).read_text(encoding="utf-8")
    # Streamlit 靠「字符串里有没有换行」区分内联代码和文件路径：
    # 单行内容会被当成路径，然后报 "must be declared in pyproject.toml with asset_dir"。
    # 压缩过的构建产物会踩这个坑，所以在读取时就挡住。
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
            "annual_report_chat",
            html=_read("chat.html"),
            css=_read("chat.css"),
            js=_read("chat.js"),
        ))
    return _cache[1]


def chat_surface(
    data: dict,
    *,
    key: str = "chat",
    on_ask: Callable[[], None] | None = None,
    on_open_page: Callable[[], None] | None = None,
    on_theme: Callable[[], None] | None = None,
):
    """挂载对话面板，返回 ComponentResult。

    回调即使不用也要传：Streamlit 只为「有回调的事件名」生成 result 属性，
    不传就拿不到 result.ask。主题状态键同理 —— 前端会 setStateValue("theme")，
    没有对应的 on_theme_change，Python 侧的写入会被静默忽略。
    """
    return _renderer()(
        key=key,
        data=data,
        width="stretch",
        height="content",
        on_ask_change=on_ask or _noop,
        on_open_page_change=on_open_page or _noop,
        on_theme_change=on_theme or _noop,
    )


def _noop() -> None:
    return None
