"""跨口径计算（rag/caliber.py）和它在复核、回退里的衔接（跨报告回答第三期，不调模型）。"""
from rag import reviewer
from rag.caliber import cross_span, facts, note_lines
from ui.payload import ai_message
from ui.turn import step

ANNUAL = {"src": "精达股份 2025 年报",
          "t": "营业收入：2025 年 246.74 亿元；2024 年 223.23 亿元；本期比上年同期增减 +10.53%"}
Q3 = {"src": "精达股份 2026 三季报",
      "t": "营业收入：2026 年前三季度 180.00 亿元；2025 年前三季度 170.00 亿元；本期比上年同期增减 +5.88%；"
           "2026 年第三季度单季 62.00 亿元（同比 +3.00%）"}
ASSETS = {"src": "精达股份 2026 三季报", "t": "总资产：2026 年前三季度末 300.00 亿元；2025 年末 280.00 亿元"}
HITS = [{"text": "营业收入 2025年 24,673,670,269.16 元 本期比上年同期增减 10.53%", "page_start": 7, "page_end": 7}]
FACTS = facts([ANNUAL, Q3, ASSETS])

TABLE = """| 项目 | 2025 年报（全年） | 2026 三季报（前三季度） | 变化 |
|---|---|---|---|
| 营业收入（亿元） | 246.74 [1] | 180.00 [2] | -27.05% |"""


def test_facts_read_span_and_year():
    got = {(f["year"], f["span"], round(f["value"] / 1e8, 2)) for f in FACTS}
    assert got == {("2025", "FY", 246.74), ("2024", "FY", 223.23), ("2026", "Q3", 180.0),
                   ("2025", "Q3", 170.0), ("2026", "单季", 62.0)}


def test_point_in_time_items_are_ignored():
    """期末对期末本来就能比：总资产不进口径检查。"""
    assert not [f for f in FACTS if f["label"] == "总资产"]


def test_growth_rate_across_spans_is_hard():
    found = cross_span(TABLE, HITS, FACTS)
    assert [(c["num"], c["how"], c["hard"]) for c in found] == [("27.05%", "增长率", True)]


def test_difference_across_years_is_hard_same_year_is_a_clue():
    """2025 全年 − 2026 前三季度：一定错；2025 全年 − 2025 前三季度 = 第四季度单季：可能合法。"""
    found = {c["num"]: c["hard"] for c in cross_span("差额 66.74 亿元；第四季度约 76.74 亿元。", HITS, FACTS)}
    assert found == {"66.74 亿元": True, "76.74 亿元": False}


def test_same_span_growth_and_quoted_numbers_pass():
    """同口径的同比（前三季度对前三季度）、片段里原样有的数都不算。"""
    assert cross_span("前三季度同比增长 5.88%，全年同比增长 10.53%。", HITS, FACTS) == []


def test_unrelated_items_never_pair():
    """只配同一科目：营业收入和净利润凑出来的百分比不算。"""
    other = {"src": "精达股份 2026 三季报", "t": "归属于上市公司股东的净利润：2026 年前三季度 6.00 亿元"}
    assert cross_span("净利润只有营收的 -97.57%", HITS, facts([ANNUAL, other])) == []


def test_note_lines_skip_supplementary_text():
    hits = [{"note": True, "note_src": "精达股份 2025 年报", "text": ANNUAL["t"]},
            {"note": True, "extra": True, "note_src": "精达股份 2026 半年报", "text": "营业收入：原文"},
            {"text": "主报告片段"}]
    assert note_lines(hits) == [{"src": "精达股份 2025 年报", "t": ANNUAL["t"]}]


# ---------------------------------------------------------------- 复核

def _res(verdict="pass", **kw):
    d = {"verdict": verdict, "points": [{"point": "营业收入", "ok": True}],
         "unsupported": [], "missed": False, "queries": [], "note": ""}
    d.update(kw)
    return d


def test_hard_cross_span_forces_regenerate_with_note():
    clues = reviewer.program_checks(TABLE, HITS, FACTS)
    out = reviewer.normalize(_res("pass"), clues)
    assert out["verdict"] == "regenerate" and "27.05%" in out["note"]
    out["clues"] = clues
    assert "口径不同不能这样比" in reviewer.feedback_block(out)


def test_soft_cross_span_is_only_a_clue():
    clues = reviewer.program_checks("第四季度约 76.74 亿元。", HITS, FACTS)
    assert reviewer.normalize(_res("pass"), clues)["verdict"] == "pass"
    assert "推算单季" in reviewer._user_message("q", None, "a", HITS, clues)


def test_report_must_be_one_we_offered():
    offered = ["2026 半年报", "2025 年报"]
    out = reviewer.normalize(_res("retrieve", queries=["毛利率 变动原因"], report="2026 半年报"), {}, offered)
    assert out["report"] == "2026 半年报"
    out = reviewer.normalize(_res("retrieve", queries=["x"], report="2024 半年报"), {}, offered)
    assert out["report"] == ""
    # 不是补检索时 report 没有意义
    assert reviewer.normalize(_res("regenerate", report="2026 半年报"), {}, offered)["report"] == ""


def test_snippets_say_which_report():
    hits = [{"note": True, "note_src": "精达股份 2026 半年报", "text": "毛利率下降"}]
    assert "（来自 精达股份 2026 半年报）" in reviewer._snippets(hits)


def test_decide_counts_main_step_when_prestep_ran():
    """分步回答：先读用了 50 秒、主步骤 30 秒 → 50+30+30 ≤ 120，可以回退；原来 80×2 > 90 不行。"""
    res = _res("regenerate")
    assert reviewer.decide(res, round_no=0, elapsed=80, budget=90) == "accept"
    assert reviewer.decide(res, round_no=0, elapsed=80, main_elapsed=30, turn_budget=120) == "regenerate"
    assert reviewer.decide(res, round_no=0, elapsed=95, main_elapsed=30, turn_budget=120) == "accept"
    assert reviewer.decide(res, round_no=1, elapsed=40, main_elapsed=10, turn_budget=120) == "accept"


# ---------------------------------------------------------------- 状态机与界面

def _turn(**kw):
    t = {"q": "毛利率为什么下降", "hits": HITS, "queries": [], "status": "reviewing", "a": "旧回答",
         "after_review": "done", "started": 1000.0}
    t.update(kw)
    return t


def _run(turn, res, now):
    return step(turn, gen=lambda *a, **k: "x", ana=lambda *a, **k: ("", {}),
                rev=lambda *a, **k: res, now=lambda: now)


def test_retrieve_in_other_report_carries_report():
    res = _res("retrieve", queries=["毛利率 变动原因"], report="2026 半年报", note="原因在半年报")
    new, entry, err = _run(_turn(), res, now=1010.0)
    assert (new["status"], new["retry_report"], new["retry_queries"]) == \
        ("retrieving", "2026 半年报", ["毛利率 变动原因"])
    msg = ai_message(new)
    assert "到 2026 半年报 里补充检索" in msg["statusText"]


def test_step_uses_main_started_for_budget():
    res = _res("regenerate", note="x")
    # 从提问起 80 秒，主步骤只用了 30 秒：分步回答还能回退一次
    new, _, _ = _run(_turn(main_started=1050.0), res, now=1080.0)
    assert new["status"] == "generating"
    # 没有分步时照旧：80×2 > 90，不回退
    new, entry, _ = _run(_turn(), res, now=1080.0)
    assert new["status"] == "done" and entry["review"]["verdict"] == "fail"
