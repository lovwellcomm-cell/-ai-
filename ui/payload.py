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
import time
from typing import Any

from rag.generator import cited_numbers
from rag.healthcheck import RULES as HEALTH_RULES
from rag.periods import FY, RANK, SHORT, TITLE, period_of
from ui.card import CARD_KEYS
from ui.glossary import payload as glossary_payload
from ui.turn import conversation_key

# 组件不做流式输出（Streamlit 的 rerun 模型做逐字流式会卡顿、闪烁），
# 等待时间由这几档真实状态文案填补。
STATUS_TEXT = {
    "resolving": "正在理解问题…",
    "retrieving": "检索相关片段…",
    # 分步读别的报告；具体是哪一步由回合上的 step_text 覆盖
    "prestep": "正在先读另一份报告…",
    "history": "正在调取往年年报，把趋势拉长到近 6 年…",
    "reviewing": "正在复核回答…",
    "generating": "正在生成回答…",
    # 首轮回答已经显示出来了，这一步只是追加「为什么」的扩展分析
    "analyzing": "正在扩展分析…",
    # 年报里没有这条信息，正在调用 Search / Fetch 补充公开信息
    "searching": "正在查询公开信息…",
    # 「几件大事」第二段：取官方行情、联网查近况、估算
    "latest": "正在查行情和最新动态…（约半分钟）",
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
        # 事实笔记片段来自别的报告：原页下面要写那份报告的名字，不是当前文档
        if hit.get("note") and hit.get("note_src"):
            sources[-1]["from"] = hit["note_src"]
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
        # 读秒的起点由服务器算好「已经过去几秒」下发，前端接着往上数 ——
        # 不下发绝对时间戳，免得服务器和浏览器的时钟差把读秒带偏。
        if turn.get("started"):
            msg["elapsed"] = max(0, int(time.time() - turn["started"]))
        if turn.get("kind") == "industry" and status == "generating":
            msg["statusText"] = "正在联网检索行业动态…（约半分钟）"
        if turn.get("kind") == "health" and status == "retrieving":
            msg["statusText"] = "正在逐项翻查报告做体检…"
        # 分步读别的报告（第二期）：app.py 每一步写好 step_text（「第 1 步：读 2025 年报…」）
        if status == "prestep" and turn.get("step_text"):
            msg["statusText"] = turn["step_text"]
        if turn.get("kind") == "drivers" and status == "retrieving":
            msg["statusText"] = "正在从年报里找最能左右业绩的几件事…（第一次约半分钟）"
        # 复核没通过、正在回退：说清楚发现了什么、在做什么（旧回答还显示着）
        if turn.get("retry") and status in ("resolving", "retrieving", "history", "generating"):
            note = (turn.get("review_last") or {}).get("note") or "回答有遗漏"
            doing = {"retrieve": "正在补充检索后重新回答", "regenerate": "正在重新生成",
                     "replan": "正在重新理解问题"}.get(turn["retry"], "正在重新回答")
            # 第三期：去别的报告补检索时说清楚去了哪份
            if turn["retry"] == "retrieve" and turn.get("retry_report"):
                doing = f"正在到 {turn['retry_report']} 里补充检索后重新回答"
            msg["statusText"] = f"复核发现：{note}。{doing}…"
            msg["retrying"] = True
    elif isinstance(turn.get("secs"), int):
        msg["secs"] = turn["secs"]
    if error:
        msg["error"] = error
    if page:
        msg["page"] = page
    # 分步记录：完成的条目里叫 steps，进行中的回合里叫 steps_log
    steps = turn.get("steps") or turn.get("steps_log")
    if steps:
        msg["steps"] = [{"src": x.get("src", ""), "n": int(x.get("n") or 0), "status": x.get("status", "")}
                        for x in steps]
    if turn.get("by_points"):
        msg["byPoints"] = [{"point": x.get("point", ""), "n": int(x.get("n") or 0)} for x in turn["by_points"]]
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
    # 结构化财务卡片（ui/card.py 产出）：组件收到就用、收不到就不渲染。
    # cardTitle / cardNote 跟着消息走 —— 全局默认值是「演示数据」，不能挂到真实数字上。
    # tableCheck：回答里 Markdown 表格的逐格核对结果（ui/turn.py:history_entry 算好存下）。
    for key in CARD_KEYS + ("tableCheck", "table", "demo", "kind", "options", "understood", "review", "style"):
        if turn.get(key):
            msg[key] = turn[key]
    if isinstance(msg.get("health"), dict):
        msg["health"] = health_card(msg["health"])
    if isinstance(msg.get("drivers"), dict):
        msg["drivers"] = drivers_card(msg["drivers"])
    return msg


def _s(v, n: int) -> str:
    return str(v if v is not None else "")[:n]


def _page(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def drivers_card(c: dict) -> dict:
    """「几件大事」卡片下发前清洗：浏览器存档带回来的数据由访客控制，
    只留认识的字段，字符串截长度，页码和数字核类型。"""
    items = []
    for it in (c.get("items") or [])[:5]:
        if not isinstance(it, dict):
            continue
        ev = [{"page": _page(e.get("page")), "quote": _s(e.get("quote"), 120)}
              for e in (it.get("evidence") or [])[:2] if isinstance(e, dict) and _page(e.get("page"))]
        ex = it.get("exposure") if isinstance(it.get("exposure"), dict) else None
        now = it.get("now") if isinstance(it.get("now"), dict) else None
        if now:
            pts = {k: {"date": _s((now.get(k) or {}).get("date"), 10),
                       "value": _num((now.get(k) or {}).get("value")),
                       "contract": _s((now.get(k) or {}).get("contract"), 12)}
                   for k in ("latest", "base") if isinstance(now.get(k), dict)}
            now = {"name": _s(now.get("name"), 20), "source": _s(now.get("source"), 20),
                   "basis": _s(now.get("basis"), 20), "unit": _s(now.get("unit"), 12),
                   "change_pct": _num(now.get("change_pct")), "change_bp": _num(now.get("change_bp")),
                   **pts}
        est = it.get("estimate") if isinstance(it.get("estimate"), dict) else {}
        sens = est.get("sensitivity") if isinstance(est.get("sensitivity"), dict) else None
        items.append({
            "type": _s(it.get("type"), 4), "name": _s(it.get("name"), 30), "how": _s(it.get("how"), 200),
            "evidence": ev,
            "exposure": {"page": _page(ex.get("page")), "text": _s(ex.get("text"), 100)} if ex else None,
            "now": now,
            "news": [_s(x, 220) for x in (it.get("news") or [])[:2] if isinstance(x, str)],
            "note": _s(it.get("note"), 80),
            "estimate": {
                "status": est.get("status") if est.get("status") in ("ok", "sensitivity", "none") else "none",
                "formula": _s(est.get("formula"), 120), "why": _s(est.get("why"), 80),
                "assume": _s(est.get("assume"), 160), "share_of": _s(est.get("share_of"), 8),
                "share": _num(est.get("share")),
                "sensitivity": {"text": _s(sens.get("text"), 80), "share": _num(sens.get("share"))} if sens else None,
            },
        })
    return {"date": _s(c.get("date"), 10), "items": items}


def health_card(h: dict) -> dict:
    """体检卡下发前补回每项的判断标准（历史里不存这段固定文字）。浏览器存档带回来的数据
    由访客控制：只留认识的 key 和灯色，其余字段一律转成有长度上限的字符串。"""
    items = []
    for it in (h.get("items") or [])[:len(HEALTH_RULES)]:
        if not isinstance(it, dict) or it.get("key") not in HEALTH_RULES:
            continue
        page = it.get("page")
        items.append({
            "key": it["key"],
            "label": str(it.get("label") or "")[:20],
            "status": it.get("status") if it.get("status") in ("red", "yellow", "green", "none") else "none",
            "value": str(it.get("value") or "")[:40],
            "summary": str(it.get("summary") or "")[:300],
            "detail": str(it.get("detail") or "")[:200],
            "page": page if isinstance(page, int) and not isinstance(page, bool) and page > 0 else None,
            "rule": HEALTH_RULES[it["key"]],
        })
    return {"period": str(h.get("period") or ""), "items": items}


def build_messages(history: list[dict], turn: dict | None = None, page: dict | None = None,
                   doc_labels: dict[str, str] | None = None) -> list[dict]:
    """已完成的历史轮次 + 进行中的一轮（如果有）。

    page 是「查看原页」渲染出来的那一页，只属于最后一条助手消息 ——
    抽屉里展示的也永远是最后一条的引用。例外是体检卡点开的原页（带 turn = 历史里第几轮）：
    挂在那一轮的消息上，卡片在那一项下面直接展开图片。

    doc_labels（文档 id → 「七匹狼 2024」）：同一公司的多个年度共用一段对话，
    **只有当这段对话真的跨了不止一份年报时**，才给每条回答标上依据的年报。
    只问过一份年报时不标，免得每条回答都挂一个重复的标签。
    """
    rounds = list(history) + ([turn] if turn and turn.get("q") else [])
    used = {r.get("doc_id") for r in rounds if r.get("doc_id")}
    labels = doc_labels if (doc_labels and len(used) > 1) else {}

    def _labelled(msg: dict, source: dict) -> dict:
        label = labels.get(source.get("doc_id", ""))
        if label:
            msg["docLabel"] = label
        return msg

    messages: list[dict] = []
    for past in history:
        messages.append({"role": "user", "text": past.get("q", "")})
        messages.append(_labelled(ai_message(past), past))
    k = page.get("turn") if page else None
    if isinstance(k, int) and 0 <= k < len(history):
        messages[2 * k + 1]["page"] = page
        page = None
    if turn and turn.get("q"):
        messages.append({"role": "user", "text": turn["q"]})
        messages.append(_labelled(ai_message(
            turn, status=turn.get("status", "retrieving"),
            error=turn.get("error", ""), page=page,
        ), turn))
    elif page and messages and messages[-1]["role"] == "ai":
        messages[-1]["page"] = page
    return messages


def report_menu(docs, current: dict) -> list[dict]:
    """顶栏下拉：当前公司已导入的报告，按年份分组（新 → 旧），同一年按一季报 → 年报排。

    [{"year": "2026", "items": [{"id", "label": "三季报", "current": True}, …]}, …]
    「同一公司」用和对话分段一样的键（ui/turn.conversation_key），保证下拉里列的
    正好是这段对话能切过去的那些报告。
    """
    key = conversation_key(current)
    mine = [d for d in docs if conversation_key(d) == key]
    groups: dict[str, list[dict]] = {}
    for d in sorted(mine, key=lambda d: (str(d.get("year", "")), RANK[period_of(d)]),
                    reverse=True):
        groups.setdefault(str(d.get("year") or "—"), []).append(d)
    return [
        {"year": year,
         "items": [{"id": d["doc_id"], "label": SHORT[period_of(d)],
                    "current": d["doc_id"] == current.get("doc_id")}
                   for d in sorted(items, key=lambda d: RANK[period_of(d)])]}
        for year, items in groups.items()
    ]


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
    reports: list[dict] | None = None,
    persist: dict | None = None,
    restore_wanted: bool = False,
    style_switch: bool = True,
) -> dict:
    """组装下发给组件的完整 data。"""
    company = doc.get("company") or ""
    year = doc.get("year") or ""
    name = doc.get("name") or ""
    pages = doc.get("pages") or 0
    chunks = doc.get("chunks") or 0
    who = f"{company} {year}".strip()
    period = period_of(doc)
    # 顶栏「精达股份 2026 三季报」；年报保持「精达股份 2025」
    shown_year = f"{year} {SHORT[period]}" if year and period != FY else year

    return json_safe({
        "theme": "dark" if theme == "dark" else "light",
        "busy": bool(busy),
        "warn": warn,
        "hint": hint,
        "model": model,
        "embedModel": embed_model,
        "cardTitle": card_title,
        "cardNote": card_note,
        "heroEyebrow": f"{who} {TITLE[period]} · 已建索引" if who else "报告已建索引",
        "heroFoot": (
            f"{pages} 页全文已切分为 {chunks} 个片段，章节、页码与报表名一并保留；"
            "提问优先检索当前选中的年报，问到别的公司时会先导入它的年报。"
        ) if pages else "",
        "placeholder": f"就《{name}》提问，例如：营业收入和归母净利润分别是多少？",
        "placeholderShort": "就当前年报提问，例如：营业收入是多少？",
        "doc": {
            "company": company, "year": shown_year, "name": name,
            "pages": int(pages), "chunks": int(chunks),
            # 巨潮导入的文档在顶栏显示来源；本地上传没有这个字段
            "source": doc.get("source_site") or doc.get("source") or "",
        },
        "messages": messages,
        # 顶栏下拉：这家公司已导入的各年度、各期报告（report_menu 的输出）
        "reports": reports or [],
        # 浏览器存档：persist 只在历史有变化时才带（{"key": 对话键, "entries": […]}），
        # restoreWanted 为真时组件把 localStorage 里的存档送回来一次。
        "persist": persist,
        "restoreWanted": bool(restore_wanted),
        # 回答风格：style_switch 为假（回滚开关 ANSWER_STYLE=pro）时界面不显示「通俗 / 专业」切换
        "styleSwitch": bool(style_switch),
        "styleDefault": "plain" if style_switch else "pro",
        # 通俗版回答里的术语解释（ui/glossary.py，程序维护的固定词表）
        "glossary": glossary_payload() if style_switch else [],
        "docId": doc.get("doc_id") or "",
    })
