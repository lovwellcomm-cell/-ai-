"""把会话状态与检索结果组装成组件需要的 data 结构。

这里刻意保持纯函数、不依赖 Streamlit —— app.py 本身没有测试覆盖，
把逻辑挤到这个模块能把不可测的胶水层压到最薄，剩下的都能单测。

一次提问的生命周期（见 app.py 的回合状态机）：
    retrieving -> generating -> done
组件按 status 决定显示「检索相关片段…」「已改写检索词…正在生成回答…」还是完整回答。

2026-10: 加入自动导入文档的来源字段（巨潮徽章）与「优先检索当前年报」的文案。
"""
from __future__ import annotations

import json
from typing import Any

from rag.generator import cited_numbers

# 组件不做流式输出（Streamlit 的 rerun 模型做逐字流式会卡顿、闪烁），
# 等待时间由这几档真实状态文案填补。
STATUS_TEXT = {
    "resolving": "正在查找年报…",
    "retrieving": "检索相关片段…",
    "generating": "正在生成回答…",
    # 首轮回答已经显示出来了，这一步只是追加「为什么」的扩展分析
    "analyzing": "正在扩展分析…",
    # 年报里没有这条信息，正在调用 Search / Fetch 补充公开信息
    "searching": "正在查询公开信息…",
}


def to_sources(hits: list[dict], answer: str) -> list[dict]:
    """检索到的片段 -> 组件用的来源列表，并标出回答真正引用过的那几段。

    编号规则与 rag/generator.py 的 format_context 一致：从 1 开始按检索顺序编号，
    回答里的 [n] 就是这个编号。
    """
    cited = set(cited_numbers(answer, len(hits)))
    sources = []
    for n, hit in enumerate(hits, start=1):
        sources.append({
            "n": n,
            "section": hit.get("section") or "正文",
            "sub": hit.get("subsection") or "",
            "p1": int(hit.get("page_start") or 0),
            "p2": int(hit.get("page_end") or 0),
            "text": hit.get("text") or "",
            "cited": n in cited,
        })
    return sources


def ai_message(turn: dict, *, status: str | None = None, error: str | None = None,
               page: dict | None = None) -> dict:
    """把一轮对话（或进行中的一轮）转成组件的助手消息。

    status / error 不传时从 turn 里读 —— 它们本来就存在 turn 上，
    让调用方再抄一遍只会多一个出错的机会。
    """
    status = status or turn.get("status") or "done"
    error = error if error is not None else (turn.get("error") or "")
    hits = turn.get("hits") or []
    answer = turn.get("a") or ""
    msg: dict[str, Any] = {
        "role": "ai",
        "status": status,
        "text": answer,
        "queries": list(turn.get("queries") or []),
        "hits": len(hits),
        "sources": to_sources(hits, answer) if hits else [],
    }
    if status != "done":
        msg["statusText"] = STATUS_TEXT.get(status, "")
    if error:
        msg["error"] = error
    if page:
        msg["page"] = page
    # 自动导入 / 自动切换文档的提示，跟着这一轮回答保留。
    if turn.get("notice"):
        msg["notice"] = turn["notice"]
    # 外部补充的网页来源。编号与正文里的 [网n] 对应，组件据此渲染可点击的链接。
    if turn.get("web_sources"):
        msg["webSources"] = [
            {
                "n": int(s.get("n") or 0),
                "title": s.get("title") or "",
                "url": s.get("url") or "",
                "site": s.get("site") or "",
                "date": s.get("date") or "",
            }
            for s in turn["web_sources"]
        ]
    # 结构化财务卡片：目前后端不产出这些字段，组件收到就用、收不到就不渲染。
    # 位置留在这里，等接入「主要会计数据」抽取后直接填即可。
    for key in ("kpis", "charts", "table", "demo"):
        if turn.get(key):
            msg[key] = turn[key]
    return msg


def build_messages(history: list[dict], turn: dict | None = None, page: dict | None = None) -> list[dict]:
    """已完成的历史轮次 + 进行中的一轮（如果有）。

    page 是「查看原页」渲染出来的那一页，只属于最后一条助手消息 ——
    抽屉里展示的也永远是最后一条的引用。
    """
    messages: list[dict] = []
    for past in history:
        messages.append({"role": "user", "text": past.get("q", "")})
        messages.append(ai_message(past))
    if turn and turn.get("q"):
        messages.append({"role": "user", "text": turn["q"]})
        messages.append(ai_message(
            turn, status=turn.get("status", "retrieving"),
            error=turn.get("error", ""), page=page,
        ))
    elif page and messages and messages[-1]["role"] == "ai":
        messages[-1]["page"] = page
    return messages


def json_safe(payload: dict) -> dict:
    """组件桥走 JSON。一旦 payload 里有 set / Path / numpy 标量 / datetime 之类，
    Streamlit 的兜底是把整个 data 静默变成一段 Python repr 字符串 ——
    前端只会看到 undefined，且不报错。所以在挂载前先自检，让它响亮地失败。
    """
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def build_data(
    *,
    doc: dict,
    messages: list[dict],
    theme: str = "light",
    busy: bool = False,
    warn: str = "",
    hint: str = "",
    model: str = "",
    embed_model: str = "",
    card_title: str = "主要会计数据",
    card_note: str = "演示数据 · 非年报原文",
) -> dict:
    """组装下发给组件的完整 data。"""
    company = doc.get("company") or ""
    year = doc.get("year") or ""
    name = doc.get("name") or ""
    pages = doc.get("pages") or 0
    chunks = doc.get("chunks") or 0
    who = f"{company} {year}".strip()

    return json_safe({
        "theme": "dark" if theme == "dark" else "light",
        "busy": bool(busy),
        "warn": warn,
        "hint": hint,
        "model": model,
        "embedModel": embed_model,
        "cardTitle": card_title,
        "cardNote": card_note,
        "heroEyebrow": f"{who} 年度报告 · 已建索引" if who else "年报已建索引",
        "heroFoot": (
            f"{pages} 页全文已切分为 {chunks} 个片段，章节、页码与报表名一并保留；"
            "提问优先检索当前选中的年报，问到别的公司时会先导入它的年报。"
        ) if pages else "",
        "placeholder": f"就《{name}》提问，例如：营业收入和归母净利润分别是多少？",
        "placeholderShort": "就当前年报提问，例如：营业收入是多少？",
        "doc": {
            "company": company, "year": year, "name": name,
            "pages": int(pages), "chunks": int(chunks),
            # 巨潮导入的文档在顶栏显示来源；本地上传没有这个字段
            "source": doc.get("source_site") or doc.get("source") or "",
        },
        "messages": messages,
    })
