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


def test_sources_name_the_other_report_for_note_hits():
    from ui.payload import to_sources
    note_hit = dict(HIT_P7, note=True, doc_id=DOC_FY, note_src="精达股份 2025 年报",
                    section="前几轮已核对的数据 · 精达股份 2025 年报")
    got = to_sources([HIT_P16, note_hit], "见 [2]")
    assert "from" not in got[0] and got[1]["from"] == "精达股份 2025 年报" and got[1]["cited"]


def test_planner_stays_on_current_report_when_notes_cover_a_cross_report_question():
    from datetime import date
    from rag.planner import normalize
    facts = [{"q": "近三年怎么样", "facts": "精达股份 2025 年报（扣非净利润）"}]
    q = "扣非净利润的 2025 年全年和 2026 年前三季度分别是多少？"
    data = {"intent": "report", "company": "", "year": None, "period": None}
    assert normalize(data, q, facts, date(2026, 11, 3))["target"]["year"] is None
    # 没有笔记：照旧按规则取问题里的年份和期
    assert normalize(data, q, [{"q": "近三年怎么样"}], date(2026, 11, 3))["target"]["year"] == 2025
    # 只问一个年份：笔记里没有，照旧换报告
    one = normalize(data, "2025 年前三季度扣非净利润是多少", facts, date(2026, 11, 3))["target"]
    assert (one["year"], one["period"]) == (2025, "Q3")


def test_rows_with_percentage_points_keep_their_source():
    """线上实测：「减少 0.13 个百分点」被当成金额 0.13 元去找出处，毛利率、ROE 行整行被丢。"""
    hit = {"text": "主营业务毛利率(%) 5.83 毛利率比上年增减 减少0.13个百分点", "page_start": 188, "page_end": 188}
    answer = ("| 项目 | 2025年 | 变化 | 来源 |\n|---|---|---|---|\n"
              "| 主营业务毛利率 | 5.83% | 减少0.13个百分点 | [2] |\n")
    got = notes.from_answer(answer, [HIT_P7, hit], [], DOC_FY, "s")
    assert [(n["p"], n["t"]) for n in got] == [(188, "主营业务毛利率：2025年 5.83%；变化 减少0.13个百分点")]


def test_merge_drops_rows_whose_amounts_are_already_noted_on_that_page():
    card = notes.from_financials(FIN, DOC_FY, "s")
    rows = notes.from_answer(ANSWER, [HIT_P7, HIT_P16], ["9.99 亿元"], DOC_FY, "s")
    got = notes.merge(card, rows)
    texts = [n["t"] for n in got]
    assert sum(t.startswith("营业收入") for t in texts) == 1        # 表格那行和卡片重复
    assert any(t.startswith("主营业务毛利率") for t in texts)       # 只有比率的行照留


# ---------------------------------------------------------------- 第二期：分步读别的报告

def test_multi_report_guard():
    from rag.planner import multi_report
    assert multi_report("对比 2025 年报和 2026 三季报")
    assert multi_report("近三年和今年前三季度比怎么样")
    assert multi_report("结合最新季报对比一下")
    assert not multi_report("营业收入是多少")
    assert not multi_report("前三季度营收同比增长多少")


def test_also_read_is_validated_and_guarded():
    from datetime import date
    from rag.planner import normalize
    today = date(2026, 11, 3)
    data = {"intent": "report", "company": "", "year": None, "period": None,
            "also_read": [{"year": 2025, "period": "FY", "need": ["营业收入"]},
                          {"year": "去年", "period": "FY"},              # 不是具体年份
                          {"year": 2030, "period": "Q3"},                # 晚于今年
                          {"year": 2025, "period": "FY", "need": ["营业收入"]}]}
    got = normalize(data, "结合 2025 年报对比一下", [], today)["also_read"]
    assert got == [{"year": 2025, "period": "FY", "need": ["营业收入"]}]
    # 问题本身不涉及两份报告：模型列了也不算
    assert normalize(data, "营业收入是多少", [], today)["also_read"] == []
    # 体检、行业动态、反问不分步
    assert normalize(dict(data, intent="industry"), "结合 2025 年报对比一下", [], today)["also_read"] == []


def test_step_notes_come_first_in_note_hits():
    step = [{"doc": DOC_Q3, "src": "精达股份 2026 三季报", "p": 3, "t": "营业收入：2026 年前三季度 234.31 亿元"}]
    hist = [{"q": "x", "notes": [{"doc": DOC_FY, "src": "精达股份 2025 年报", "p": 7, "t": "营业收入：2025 年 246.74 亿元"}]}]
    got = notes.as_hits(hist, "c" * 16, extra=step)
    assert [h["doc_id"] for h in got] == [DOC_Q3, DOC_FY]


def test_covered_by_financials():
    assert notes.covered_by_financials(["营业收入", "归母净利润", "经营现金流"])
    assert notes.covered_by_financials([])
    assert not notes.covered_by_financials(["营业收入", "毛利率"])


def test_steps_are_recorded_archived_and_sent_to_the_ui():
    from ui.payload import ai_message
    log = [{"src": "精达股份 2025 年报", "n": 7, "status": "ok"},
           {"src": "精达股份 2026 三季报", "n": 0, "status": "main"}]
    entry = history_entry({"q": "对比", "hits": [HIT_P7], "queries": [], "doc_id": DOC_Q3,
                           "src_label": "精达股份 2026 三季报", "steps_log": log,
                           "step_notes": notes.from_financials(FIN, DOC_FY, "精达股份 2025 年报")}, "好。")
    assert entry["steps"] == log and entry["notes"][0]["doc"] == DOC_FY
    back = memory.clean_entry(memory.pack([entry])[0], {DOC_FY, DOC_Q3})
    assert back["steps"] == log
    bad = memory.clean_entry(dict(memory.pack([entry])[0], steps=[{"src": "x", "n": True, "status": "ok"},
                                                                   {"src": "x", "n": 1, "status": "?"}]),
                             {DOC_FY, DOC_Q3})
    assert "steps" not in bad
    assert ai_message(entry)["steps"][0]["n"] == 7
    assert ai_message({"q": "x", "status": "prestep", "step_text": "第 1 步：先读 2025 年报…"})["statusText"] \
        == "第 1 步：先读 2025 年报…"


def test_two_reports_in_question_keep_the_models_main_report():
    """「对比 2025 年报和 2026 三季报」：规则会拼出 2025 三季报，有 also_read 时主报告听模型的。"""
    from datetime import date
    from rag.planner import multi_report, normalize
    data = {"intent": "report", "company": "精达", "year": 2026, "period": "Q3",
            "also_read": [{"year": 2025, "period": "FY", "need": ["营业收入"]}]}
    got = normalize(data, "对比一下精达 2025 年报和 2026 三季报的营收", [], date(2026, 11, 3))
    assert (got["target"]["year"], got["target"]["period"]) == (2026, "Q3")
    assert multi_report("一季度和三季度的营收分别是多少")


def test_read_step_uses_financials_only_when_they_cover_the_need():
    calls = []
    kw = dict(fin=FIN, search=lambda q: calls.append("search") or [HIT_P16],
              gen=lambda q, h: calls.append("gen") or "| 项目 | 2025年 | 来源 |\n|---|---|---|\n| 主营业务毛利率 | 5.83% | [1] |\n",
              check=lambda a, h: {"unverified": []})
    got = notes.read_step(DOC_FY, "精达股份 2025 年报", "q", ["营业收入", "归母净利润"], **kw)
    assert calls == [] and len(got) == 2
    got = notes.read_step(DOC_FY, "精达股份 2025 年报", "q", ["营业收入", "毛利率"], **kw)
    assert calls == ["search", "gen"] and got[-1]["t"] == "主营业务毛利率：2025年 5.83%" and got[-1]["p"] == 16


def test_quarter_report_notes_include_the_single_third_quarter():
    fin = {"period": "Q3", "page": 1, "years": ["2026", "2025"],
           "items": {"revenue": {"label": "营业收入", "change": 31.81,
                                 "values": {"2026": 23430538137.40, "2025": 17776000000.0},
                                 "quarter": {"value": 8129474578.52, "change": 37.31}}}}
    t = notes.from_financials(fin, DOC_Q3, "s")[0]["t"]
    assert "2026 年第三季度单季 81.29 亿元（同比 +37.31%）" in t


def test_trend_keeps_the_planners_main_report_when_the_annual_is_read_first():
    from ui.turn import plan_for
    docs = [{"doc_id": "jd25", "company": "精达股份", "year": "2025"},
            {"doc_id": "jd26q3", "company": "精达股份", "year": "2026", "period": "Q3"}]
    u = {"intent": "trend", "target": {"company": "精达股份", "year": 2026, "period": "Q3"},
         "also_read": [{"year": 2025, "period": "FY", "need": []}]}
    kw = dict(current_company="精达股份", current_year="2025", current_period="FY", docs=docs)
    assert plan_for(u, **kw)["doc"]["doc_id"] == "jd26q3"
    # 没有先读年报：趋势题照旧改用年报
    assert plan_for(dict(u, also_read=[]), **kw)["action"] == "current"
