"""对话的「记忆」：存到访客浏览器里（刷新不丢），以及追问时带给模型的上一轮结论摘要。

两件事都是纯函数，不碰 Streamlit：

1. pack / unpack —— 历史条目 ↔ 存进浏览器 localStorage 的形状。
   浏览器里的数据**由访客控制**，回来时一律当不可信输入：只收白名单字段、类型和长度都校验，
   文档 id 必须是服务器上真有的，否则丢掉那个字段（回答照样能显示，只是不能翻原页）。
2. answer_gist / recent_context —— 上一轮回答的结论，去掉数字和引用编号。
   只用来让模型理解「那这个原因呢」「它为什么下降」里指的是什么；数字不带过去，
   上一轮算错的数会顺着污染这一轮，这一轮的数字仍然只能来自这一轮检索到的原文。
"""
from __future__ import annotations

import re

from rag.notes import sources_line
from ui.card import CARD_KEYS

MAX_TURNS = 12            # 每家公司在浏览器里最多留几轮
MAX_CONVS = 20            # 最多留几家公司
_DOC_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_CONV_KEY_RE = re.compile(r"^(co|doc):.{1,60}$")

_ENTRY_STR = {"q": 500, "a": 20000, "notice": 200, "kind": 20, "understood": 300}
# industry 行业动态；clarify 反问（带 options）；refuse 超出范围；health 年报体检；drivers 几件大事
_KINDS = ("industry", "clarify", "refuse", "health", "drivers")
_HIT_STR = {"text": 4000, "section": 120, "subsection": 160}
_WEB_STR = {"title": 200, "url": 600, "site": 80, "date": 20}
# 卡片、表格核对这些是程序算好的展示数据：结构复杂，原样带回，只限制总体大小
_OPAQUE = CARD_KEYS + ("tableCheck", "review")
_OPAQUE_MAX = 6000
MAX_NOTES = 20            # 每轮最多几条事实笔记（rag/notes.MAX_PER_TURN）
_STEP_STATUS = ("ok", "have", "same", "skipped", "timeout", "quota", "error", "main")
NOTE_TEXT_LIMIT = 300
# 「几件大事」每个因素带原文引用、最新动态、估算，5 个因素实测约 7–9 千字：超了会在刷新后悄悄消失
_OPAQUE_MAX_BY_KEY = {"drivers": 16000}


def _trim(v, limit: int) -> str:
    return v[:limit] if isinstance(v, str) else ""


def _clean_hit(h, known_docs: set[str] | None = None) -> dict | None:
    if not isinstance(h, dict):
        return None
    out = {k: _trim(h.get(k), n) for k, n in _HIT_STR.items()}
    try:
        out["page_start"] = max(1, int(h.get("page_start") or 1))
        out["page_end"] = max(out["page_start"], int(h.get("page_end") or out["page_start"]))
    except (TypeError, ValueError):
        return None
    # 事实笔记片段（rag/notes.as_hits）：来自别的报告，翻原页靠它的 doc_id
    did = h.get("doc_id")
    if (h.get("note") is True and isinstance(did, str) and _DOC_ID_RE.match(did)
            and (known_docs is None or did in known_docs)):
        out.update(note=True, doc_id=did, note_src=_trim(h.get("note_src"), 40))
    return out if out["text"] else None


def _clean_note(n, known_docs: set[str]) -> dict | None:
    """一条事实笔记：报告必须是本地有的，页码是正整数，正文限长。"""
    if not isinstance(n, dict):
        return None
    doc, p = n.get("doc"), n.get("p")
    if not (isinstance(doc, str) and _DOC_ID_RE.match(doc) and doc in known_docs):
        return None
    if not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 5000:
        return None
    src, text = _trim(n.get("src"), 40), _trim(n.get("t"), NOTE_TEXT_LIMIT)
    return {"doc": doc, "src": src, "p": p, "t": text} if src and text else None


def _clean_web(w) -> dict | None:
    if not isinstance(w, dict):
        return None
    out = {k: _trim(w.get(k), n) for k, n in _WEB_STR.items()}
    try:
        out["n"] = int(w.get("n") or 0)
    except (TypeError, ValueError):
        return None
    if not re.match(r"^https?://", out["url"], re.I):
        return None
    return out


def _size(v) -> int:
    return len(repr(v))


def clean_entry(e, known_docs: set[str]) -> dict | None:
    """一条从浏览器带回来的历史：白名单 + 类型 + 长度。q 和 a 必须有。"""
    if not isinstance(e, dict):
        return None
    out = {k: _trim(e.get(k), n) for k, n in _ENTRY_STR.items()}
    if not out["q"] or not out["a"]:
        return None
    for k in ("notice", "kind", "understood"):
        if not out[k]:
            out.pop(k)
    if out.get("kind") not in (None, *_KINDS):
        out.pop("kind")
    hits = [h for h in (_clean_hit(x, known_docs) for x in (e.get("hits") or [])[:16]) if h]
    out["hits"] = hits
    out["queries"] = [q[:200] for q in (e.get("queries") or [])[:6] if isinstance(q, str)]
    web = [w for w in (_clean_web(x) for x in (e.get("web_sources") or [])[:10]) if w]
    if web:
        out["web_sources"] = web
    opts = e.get("options")
    if out.get("kind") == "clarify" and isinstance(opts, list):
        out["options"] = [o[:60] for o in opts[:4] if isinstance(o, str) and o.strip()]
    secs = e.get("secs")
    if isinstance(secs, int) and not isinstance(secs, bool) and 0 <= secs <= 3600:
        out["secs"] = secs
    if e.get("style") in ("plain", "pro"):              # 回答风格：只认两个值
        out["style"] = e["style"]
    # 事实笔记（rag/notes.py）：浏览器送回来的，逐条校验
    raw_notes = e.get("notes") if isinstance(e.get("notes"), list) else []
    notes = [n for n in (_clean_note(x, known_docs) for x in raw_notes[:MAX_NOTES]) if n]
    if notes:
        out["notes"] = notes
    # 分步记录（第二期）：先读了哪几份报告、各记下几个数。只给界面那一行摘要用
    steps = []
    for x in (e.get("steps") if isinstance(e.get("steps"), list) else [])[:3]:
        n = x.get("n") if isinstance(x, dict) else None
        if (isinstance(x, dict) and x.get("status") in _STEP_STATUS and isinstance(n, int)
                and not isinstance(n, bool) and 0 <= n <= 50 and _trim(x.get("src"), 40)):
            steps.append({"src": _trim(x["src"], 40), "n": x["n"], "status": x["status"]})
    if steps:
        out["steps"] = steps
    did = e.get("doc_id")
    if isinstance(did, str) and _DOC_ID_RE.match(did) and did in known_docs:
        out["doc_id"] = did
    for k in _OPAQUE:
        v = e.get(k)
        if v and isinstance(v, (dict, list, str)) and _size(v) <= _OPAQUE_MAX_BY_KEY.get(k, _OPAQUE_MAX):
            out[k] = v
    return out


def pack(history: list[dict]) -> list[dict]:
    """要存进浏览器的形状：最近 MAX_TURNS 轮，只留重新显示和翻原页需要的字段。"""
    out = []
    for e in history[-MAX_TURNS:]:
        item = {k: e[k] for k in ("q", "a", "notice", "kind", "doc_id", "queries", "web_sources",
                                  "options", "understood", "style", "notes", "steps")
                if e.get(k)}
        if isinstance(e.get("secs"), int):
            item["secs"] = e["secs"]
        item["hits"] = [{k: h.get(k) for k in ("text", "section", "subsection",
                                                "page_start", "page_end", "note", "doc_id", "note_src")
                         if k in h}
                        for h in (e.get("hits") or [])]
        for k in _OPAQUE:
            if e.get(k):
                item[k] = e[k]
        out.append(item)
    return out


def unpack(raw, known_docs: set[str]) -> tuple[dict[str, list[dict]], str | None]:
    """浏览器送回来的 {"convs": {对话键: [条目…]}, "last": 文档id} → (历史, 上次看的文档)。"""
    if not isinstance(raw, dict):
        return {}, None
    convs = raw.get("convs")
    out: dict[str, list[dict]] = {}
    if isinstance(convs, dict):
        for key, entries in list(convs.items())[:MAX_CONVS]:
            if not isinstance(key, str) or not _CONV_KEY_RE.match(key) \
                    or not isinstance(entries, list):
                continue
            kept = [x for x in (clean_entry(e, known_docs) for e in entries[-MAX_TURNS:]) if x]
            if kept:
                out[key] = kept
    last = raw.get("last")
    last = last if isinstance(last, str) and last in known_docs else None
    return out, last


def doc_ids_in(convs: dict[str, list[dict]]) -> set[str]:
    return {e["doc_id"] for entries in convs.values() for e in entries if e.get("doc_id")}


# ---------------------------------------------------------------- 上一轮结论摘要

_TABLE_LINE = re.compile(r"^\s*\|")
_CITE = re.compile(r"\[(?:网\s*)?[\d,，、\s]+\]")
# 数字连同紧跟的单位一起去掉：「增长 10.53%」→「增长」，「246.74 亿元」→ 空
_NUMBER = re.compile(
    r"[（(]?[-+−]?\d[\d,，]*(?:\.\d+)?\s*(?:%|％|亿元|万元|千元|元|个百分点|百分点|pct|倍|亿|万|年|月|日|个)?[)）]?")
_SENT_END = re.compile(r"(?<=[。！？；])")
GIST_LIMIT = 120


def answer_gist(answer: str) -> str:
    """回答开头的结论（表格之前的一两句），去掉数字、单位和引用编号。"""
    lines = []
    for ln in (answer or "").splitlines():
        if _TABLE_LINE.match(ln):
            break
        s = ln.strip()
        if s.startswith("**") and s.endswith("**"):
            continue                                   # 小标题
        s = re.sub(r"^[-*•]\s+", "", s)
        if s:
            lines.append(s)
        if sum(len(x) for x in lines) >= GIST_LIMIT:
            break
    text = " ".join(lines)
    text = _CITE.sub("", text)
    text = _NUMBER.sub("", text)
    text = text.replace("**", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([，。；、])", r"\1", text)
    text = re.sub(r"[，、]{2,}", "，", text).strip(" ，、")
    sents = [s for s in _SENT_END.split(text) if s.strip()]
    gist = "".join(sents[:2]) if sents else text
    return gist[:GIST_LIMIT].strip()


def recent_context(history: list[dict], n: int) -> list[dict]:
    """最近 n 轮：[{"q": 问题, "gist": 结论摘要}]。行业动态那种联网段不做摘要（不是报告结论）。"""
    out = []
    for e in history[-n:] if n > 0 else []:
        item = {"q": e.get("q", "")}
        if e.get("kind") not in ("industry", "refuse"):
            g = answer_gist(e.get("a", ""))
            if g:
                item["gist"] = g
        # 这一轮记下了哪些报告的哪些数据（只有报告名和科目名）：规划器据此知道哪些不用再查
        facts = sources_line(e)
        if facts:
            item["facts"] = facts
        if item["q"]:
            out.append(item)
    return out
