"""对话记忆（ui/memory.py）：浏览器存档的打包 / 校验，和追问时带给模型的结论摘要。

浏览器里的存档由访客控制，送回服务器时当不可信输入；
结论摘要不能带数字 —— 上一轮算错的数会顺着污染这一轮。
"""
from rag.generator import _history_block
from ui.memory import answer_gist, clean_entry, pack, recent_context, unpack

DOC = "0123456789abcdef"
KNOWN = {DOC}

ENTRY = {
    "q": "营收和利润怎么样？",
    "a": "营收与利润双增，净利率小幅下滑[1][2]。\n\n| 项目 | 2025年 |\n|---|---|\n| 营业收入 | 246.74 亿元 |",
    "hits": [{"text": "营业收入 24,673,670,269.16", "section": "第二节", "subsection": "主要会计数据",
              "page_start": 7, "page_end": 7, "chunk_id": 3, "index_text": "很长的前缀文本"}],
    "queries": ["营业收入"], "doc_id": DOC, "analyze": "ok", "web": "",
    "kpis": [{"k": "营业收入", "v": "246.74", "u": "亿元"}], "cardNote": "程序抽取",
}


def test_pack_keeps_display_fields_and_drops_internals():
    item = pack([ENTRY])[0]
    assert item["q"] == ENTRY["q"] and item["doc_id"] == DOC and item["kpis"]
    assert "analyze" not in item and "chunk_id" not in item["hits"][0]
    assert "index_text" not in item["hits"][0]


def test_pack_keeps_only_recent_turns():
    many = [dict(ENTRY, q=f"问题{i}") for i in range(30)]
    packed = pack(many)
    assert len(packed) == 12 and packed[-1]["q"] == "问题29"


def test_round_trip():
    convs, last = unpack({"convs": {"co:精达": pack([ENTRY])}, "last": DOC}, KNOWN)
    assert last == DOC
    e = convs["co:精达"][0]
    assert e["q"] == ENTRY["q"] and e["hits"][0]["page_start"] == 7 and e["doc_id"] == DOC


def test_unknown_doc_id_is_dropped_but_answer_kept():
    convs, last = unpack({"convs": {"co:精达": [dict(ENTRY, doc_id="ffffffffffffffff")]},
                          "last": "ffffffffffffffff"}, KNOWN)
    assert "doc_id" not in convs["co:精达"][0] and last is None


def test_tampered_payload_is_filtered():
    raw = {"convs": {
        "co:精达": [
            {"q": "x", "a": "y", "hits": [{"text": "t", "page_start": "abc"}],
             "web_sources": [{"n": 1, "url": "javascript:alert(1)", "title": "x"}],
             "kind": "evil", "extra": "不该留下"},
            "不是字典", {"q": "", "a": "没有问题"},
        ],
        "bad key": [ENTRY],
        "co:茅台": "不是列表",
    }, "last": 123}
    convs, last = unpack(raw, KNOWN)
    assert list(convs) == ["co:精达"] and last is None
    e = convs["co:精达"][0]
    assert e["hits"] == [] and "web_sources" not in e and "kind" not in e and "extra" not in e


def test_long_strings_are_truncated():
    e = clean_entry({"q": "问" * 2000, "a": "答"}, KNOWN)
    assert len(e["q"]) == 500
    assert unpack("不是字典", KNOWN) == ({}, None)


def test_gist_removes_numbers_units_and_citations():
    gist = answer_gist(ENTRY["a"])
    assert gist == "营收与利润双增，净利率小幅下滑。"
    gist = answer_gist("2025 年营业收入 246.74 亿元，同比增长 10.53%[1]；毛利率提升 0.6 个百分点[2]。")
    assert not any(ch.isdigit() for ch in gist)
    assert "营业收入" in gist and "同比增长" in gist and "[" not in gist


def test_gist_stops_at_table_and_skips_headings():
    assert answer_gist("**结论**\n| a | b |\n|---|---|") == ""
    assert answer_gist("经营现金流转正[3]。\n- 第二条") .startswith("经营现金流转正")


def test_recent_context_skips_industry_gist():
    hist = [ENTRY, {"q": "行业最新动态", "a": "**行业动态**\n- 2026-10 铜价…[网1]", "kind": "industry"}]
    ctx = recent_context(hist, 2)
    assert ctx[0]["gist"] == "营收与利润双增，净利率小幅下滑。"
    assert ctx[1] == {"q": "行业最新动态"}
    assert recent_context(hist, 0) == []


def test_history_block_shows_question_and_gist():
    block = _history_block([{"q": "营收怎么样", "gist": "营收增长，利润下滑。"}, "那原因呢"])
    assert "用户：营收怎么样" in block
    assert "助手（上一轮结论摘要，已去掉数字）：营收增长，利润下滑。" in block
    assert "用户：那原因呢" in block and "数字和事实只能来自本轮" in block
    assert _history_block([]) == "" and _history_block(None) == ""


def test_secs_round_trip_and_rejects_bad_values():
    convs, _ = unpack({"convs": {"co:精达": pack([dict(ENTRY, secs=23)])}}, KNOWN)
    assert convs["co:精达"][0]["secs"] == 23
    for bad in (-1, 99999, "23", True, 2.5):
        assert "secs" not in clean_entry(dict(ENTRY, secs=bad), KNOWN)
