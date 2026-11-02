"""事实笔记（rag/notes.py）与它在回合、浏览器存档、规划器、生成提示词里的接线。"""
from rag import notes
from rag.generator import build_messages
from rag.planner import _context
from ui import memory
from ui.turn import history_entry

DOC_FY = "a" * 16          # 精达股份 2025 年报
DOC_Q3 = "b" * 16          # 精达股份 2026 三季报

FIN = {
    "period": "FY", "unit": "元", "page": 7, "years": ["2025", "2024", "2023"],
    "items": {
        "revenue": {"label": "营业收入", "change": 10.53,
                    "values": {"2025": 24673670269.16, "2024": 22322579930.98, "2023": 20539000000.0}},
        "np_parent": {"label": "归属于上市公司股东的净利润", "change": 6.66,
                      "values": {"2025": 599143119.39, "2024": 561708879.75, "2023": 500000000.0}},
    },
}

HIT_P7 = {"text": "营业收入 24,673,670,269.16 22,322,579,930.98 10.53\n"
                  "归属于上市公司股东的净利润 599,143,119.39 561,708,879.75 6.66",
          "section": "七、近三年主要会计数据和财务指标", "subsection": "", "page_start": 7, "page_end": 7}
HIT_P16 = {"text": "主营业务毛利率 5.83% 比上年减少 0.13 个百分点",
           "section": "经营情况讨论与分析", "subsection": "", "page_start": 16, "page_end": 16}

ANSWER = """2025 年营业收入 246.74 亿元[1]，同比增长 10.53%。

| 项目 | 2025 年 | 2024 年 | 同比 | 来源 |
|---|---|---|---|---|
| 营业收入 | 246.74 亿元 | 223.23 亿元 | +10.53% | [1] |
| **归母净利润** | 5.99 亿元 | 9.99 亿元 | +6.66% | [1] |
| 主营业务毛利率 | 5.83% | — | — | [2] |
"""


def test_from_financials_one_note_per_item_with_page():
    got = notes.from_financials(FIN, DOC_FY, "精达股份 2025 年报")
    assert [n["p"] for n in got] == [7, 7]
    assert got[0]["doc"] == DOC_FY and got[0]["src"] == "精达股份 2025 年报"
    assert got[0]["t"].startswith("营业收入：2025 年 246.74 亿元；2024 年 223.23 亿元")
    assert "+10.53%" in got[0]["t"]


def test_from_financials_quarter_labels_balance_items_as_period_end():
    fin = {"period": "Q3", "page": 3, "years": ["2026", "2025"],
           "items": {"revenue": {"label": "营业收入", "change": 8.0,
                                 "values": {"2026": 2e10, "2025": 1.85e10}},
                     "total_assets": {"label": "总资产", "change": 3.0,
                                      "values": {"2026": 1.5e10, "2025": 1.45e10}}}}
    got = {n["t"].split("：")[0]: n["t"] for n in notes.from_financials(fin, DOC_Q3, "x")}
    assert "2026 年前三季度 200.00 亿元" in got["营业收入"]
    assert "2026 年前三季度末" in got["总资产"] and "2025 年末" in got["总资产"]


def test_from_financials_needs_page_and_doc():
    assert notes.from_financials(dict(FIN, page=None), DOC_FY, "x") == []
    assert notes.from_financials(FIN, "", "x") == []
    assert notes.from_financials(None, DOC_FY, "x") == []


def test_from_answer_keeps_verified_cells_and_drops_unverified():
    got = notes.from_answer(ANSWER, [HIT_P7, HIT_P16], ["9.99 亿元"], DOC_FY, "精达股份 2025 年报")
    by = {n["t"].split("：")[0]: n for n in got}
    assert by["营业收入"]["p"] == 7 and by["营业收入"]["doc"] == DOC_FY
    assert "来源" not in by["营业收入"]["t"] and "[1]" not in by["营业收入"]["t"]
    # 加粗去掉；没核对通过的「9.99 亿元」不进笔记，同一行其他格子留着
    assert "9.99" not in by["归母净利润"]["t"] and "5.99 亿元" in by["归母净利润"]["t"]
    # 整行只有百分比：出处取这一行引用的片段
    assert by["主营业务毛利率"]["p"] == 16


def test_from_answer_skips_rows_without_a_source():
    answer = "| 项目 | 2025 年 |\n|---|---|\n| 研发费用 | 8.88 亿元 |\n"
    assert notes.from_answer(answer, [HIT_P7], [], DOC_FY, "x") == []


def test_from_answer_keeps_original_report_for_numbers_from_note_hits():
    note_hit = {"text": "营业收入：2025 年 246.74 亿元；2024 年 223.23 亿元", "note": True,
                "doc_id": DOC_FY, "note_src": "精达股份 2025 年报",
                "section": "前几轮已核对的数据 · 精达股份 2025 年报", "page_start": 7, "page_end": 7}
    answer = ("| 项目 | 2025 全年 | 来源 |\n|---|---|---|\n"
              "| 营业收入 | 246.74 亿元 | [2] |\n")
    got = notes.from_answer(answer, [HIT_P16, note_hit], [], DOC_Q3, "精达股份 2026 三季报")
    assert got == [{"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 7,
                    "t": "营业收入：2025 全年 246.74 亿元"}]


def test_as_hits_only_other_reports_grouped_by_page_newest_first():
    hist = [
        {"q": "近三年怎么样", "notes": [
            {"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 7, "t": "营业收入：2025 年 246.74 亿元"},
            {"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 7, "t": "归属于上市公司股东的净利润：2025 年 5.99 亿元"},
            {"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 16, "t": "主营业务毛利率：5.83%"}]},
        {"q": "三季报呢", "notes": [
            {"doc": DOC_Q3, "src": "精达股份 2026 三季报", "p": 3, "t": "营业收入：2026 年前三季度 200.00 亿元"}]},
    ]
    got = notes.as_hits(hist, DOC_Q3)
    assert [h["page_start"] for h in got] == [7, 16]
    assert got[0]["note"] is True and got[0]["doc_id"] == DOC_FY
    assert got[0]["section"] == "前几轮已核对的数据 · 精达股份 2025 年报"
    assert got[0]["text"].count("\n") == 1
    # 站在年报上：只带三季报的
    assert [h["doc_id"] for h in notes.as_hits(hist, DOC_FY)] == [DOC_Q3]


def test_as_hits_only_looks_at_last_three_turns_and_caps_pages():
    old = {"q": "很早", "notes": [{"doc": DOC_FY, "src": "s", "p": 99, "t": "x：1 亿元"}]}
    pages = {"q": "多页", "notes": [{"doc": DOC_FY, "src": "s", "p": p, "t": f"y{p}：1 亿元"}
                                    for p in range(1, 8)]}
    got = notes.as_hits([old, {"q": "a"}, {"q": "b"}, pages], DOC_Q3)
    assert len(got) == notes.MAX_NOTE_HITS and 99 not in [h["page_start"] for h in got]


def test_history_entry_records_notes_for_report_answers_only():
    turn = {"q": "营收", "hits": [HIT_P7, HIT_P16], "queries": [], "doc_id": DOC_FY,
            "src_label": "精达股份 2025 年报",
            "fin_notes": notes.from_financials(FIN, DOC_FY, "精达股份 2025 年报")}
    entry = history_entry(turn, ANSWER)
    assert entry["notes"][0]["t"].startswith("营业收入：2025 年 246.74 亿元")   # 卡片的在前
    assert len(entry["notes"]) <= notes.MAX_PER_TURN
    assert "notes" not in history_entry(dict(turn, kind="health"), ANSWER)


def test_memory_round_trip_and_rejects_bad_notes():
    entry = {"q": "营收", "a": ANSWER, "doc_id": DOC_FY,
             "hits": [HIT_P7, dict(HIT_P16, note=True, doc_id=DOC_Q3, note_src="精达股份 2026 三季报")],
             "notes": [{"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 7, "t": "营业收入：246.74 亿元"},
                       {"doc": "c" * 16, "src": "别的", "p": 7, "t": "不在本地"},
                       {"doc": DOC_FY, "src": "s", "p": True, "t": "页码是布尔"},
                       {"doc": DOC_FY, "src": "s", "p": 0, "t": "页码越界"},
                       "不是字典"]}
    packed = memory.pack([entry])[0]
    back = memory.clean_entry(packed, {DOC_FY, DOC_Q3})
    assert back["notes"] == [entry["notes"][0]]
    assert back["hits"][1]["note"] is True and back["hits"][1]["doc_id"] == DOC_Q3
    assert "note" not in back["hits"][0]
    # 笔记片段的报告不在本地：片段照留，但不能翻那份报告
    back = memory.clean_entry(packed, {DOC_FY})
    assert "doc_id" not in back["hits"][1]
    # notes 不是列表也不报错
    assert "notes" not in memory.clean_entry(dict(packed, notes={"x": 1}), {DOC_FY})


def test_planner_sees_which_reports_have_notes():
    hist = memory.recent_context([{"q": "近三年怎么样", "a": "增长。", "notes": [
        {"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 7, "t": "营业收入：2025 年 246.74 亿元"}]}], 3)
    assert hist[0]["facts"] == "精达股份 2025 年报（营业收入）"
    text = _context("结合最新季报对比一下", hist, {"company": "精达股份", "year": "2026", "period": "Q3"},
                    [], __import__("datetime").date(2026, 11, 3))
    assert "已核对的数据" in text and "246.74" not in text


def test_generator_adds_cross_report_rule_only_with_note_hits():
    plain = build_messages("营收", [HIT_P7], company="精达股份", year="2026")
    assert "前几轮已核对的数据" not in plain[0]["content"]
    note_hit = dict(HIT_P7, note=True, doc_id=DOC_FY, section="前几轮已核对的数据 · 精达股份 2025 年报")
    msgs = build_messages("对比一下", [HIT_P16, note_hit], company="精达股份", year="2026")
    assert "前几轮已核对的数据" in msgs[0]["content"]
    assert "[2]（前几轮已核对的数据 · 精达股份 2025 年报，第7页）" in msgs[1]["content"]
