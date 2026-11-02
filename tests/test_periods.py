"""定期报告的「期」：问题里问的是哪一期，以及据此切换到哪份文档（rag/periods.py、ui/turn.py）。"""
from datetime import date

import pytest

from rag import periods
from ui.turn import plan_resolution, with_period


TODAY = date(2026, 11, 2)
QDOCS = [
    {"doc_id": "fy25", "company": "精达股份", "year": "2025"},
    {"doc_id": "q3", "company": "精达股份", "year": "2026", "period": "Q3"},
    {"doc_id": "q1", "company": "精达股份", "year": "2026", "period": "Q1"},
    {"doc_id": "mt24q3", "company": "贵州茅台", "year": "2024", "period": "Q3"},
]


def qplan(question, company="", year=None, cur_year="2026", cur_period="Q3"):
    tgt = with_period({"company": company, "year": year, "multiple": False}, question, TODAY)
    return plan_resolution(tgt, current_company="精达股份", current_year=cur_year,
                           current_period=cur_period, docs=QDOCS, today=TODAY)


@pytest.mark.parametrize("q, want", [
    ("2026年前三季度营收", "Q3"), ("三季报利润怎么样", "Q3"), ("Q3 毛利率", "Q3"),
    ("1-9月现金流", "Q3"), ("上半年业绩", "H1"), ("中报怎么说", "H1"),
    ("二季度营收", "H1"), ("一季度净利润", "Q1"), ("第一季度怎么样", "Q1"),
    ("2025年报营收", "FY"), ("四季度收入", "FY"), ("全年业绩", "FY"),
    ("这份年报里营收多少", None), ("营收怎么样", None), ("去年同期呢", None),
])
def test_period_in(q, want):
    assert periods.period_in(q) == want


def test_relative_year_only_next_to_a_period():
    assert periods.relative_year("去年三季度营收", TODAY) == 2025
    assert periods.relative_year("今年一季报", TODAY) == 2026
    assert periods.relative_year("和去年同期相比", TODAY) is None


def test_latest_quarter_respects_disclosure_deadlines():
    assert periods.latest_quarter(date(2026, 11, 2)) == (2026, "Q3")
    assert periods.latest_quarter(date(2026, 10, 20)) == (2026, "H1")
    assert periods.latest_quarter(date(2026, 3, 1)) == (2025, "Q3")
    assert periods.latest_year("FY", date(2026, 3, 1)) == 2024
    assert periods.latest_year("FY", date(2026, 5, 1)) == 2025


def test_detect_period_from_cover():
    assert periods.detect("铜陵精达 2026 年第三季度报告") == ("2026", "Q3")
    assert periods.detect("2026年半年度报告全文") == ("2026", "H1")
    assert periods.detect("2025 年年度报告") == ("2025", "FY")


def test_question_without_period_stays_on_the_quarterly_doc():
    assert qplan("营收怎么样")["action"] == "current"
    assert qplan("这份年报里营收多少")["action"] == "current"


def test_other_quarter_of_the_same_year():
    """在 2026 三季报上问「一季度呢」→ 2026 一季报，而不是别的年份。"""
    out = qplan("那一季度呢")
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "q1"


def test_half_year_of_same_year_is_imported():
    assert qplan("上半年怎么样") == {"action": "import", "company": "精达股份",
                                    "year": 2026, "period": "H1"}


def test_bare_year_followup_prefers_the_annual_report():
    """在三季报上问「那2025年呢」：2025 年本地有年报，就用年报。"""
    out = qplan("那2025年呢", year=2025)
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "fy25"


def test_annual_doc_asks_for_quarter():
    out = qplan("三季度业绩", cur_year="2025", cur_period="FY")
    assert out == {"action": "import", "company": "精达股份", "year": 2025, "period": "Q3"}


def test_other_company_stale_local_quarter_is_not_reused():
    """本地只有茅台 2024 三季报，问「茅台三季报」应去拿已披露的最新一期，而不是用旧的。"""
    out = qplan("茅台三季报", company="茅台")
    assert out == {"action": "import", "company": "茅台", "year": None, "period": "Q3"}


def test_latest_quarter_question():
    tgt = with_period({"company": "", "year": None, "multiple": False}, "最新季报怎么样", TODAY)
    assert tgt["period"] == "Q3" and tgt["year"] == 2026


# ---------------------------------------------------------------- 「最新财报」

@pytest.mark.parametrize("q", ["最新财报如何", "最新的财报怎么样", "最近一期业绩", "看看最新报告",
                               "最新一期财务数据", "公司最近的业绩怎么样"])
def test_latest_any_report_phrases(q):
    assert periods.wants_latest(q) == "any"


@pytest.mark.parametrize("q", ["行业最新动态", "最近行业有什么变化", "营收怎么样", "最新三季报"])
def test_not_latest_report_phrases(q):
    assert periods.wants_latest(q) is None


def test_latest_annual_phrase():
    assert periods.wants_latest("最新年报里营收多少") == "FY"


def test_latest_report_by_period_end():
    assert periods.latest_report(date(2026, 11, 2)) == (2026, "Q3")
    # 2025 年报和 2026 一季报都刚过截止日：报告期更晚的一季报才是最新
    assert periods.latest_report(date(2026, 5, 1)) == (2026, "Q1")
    assert periods.latest_report(date(2026, 4, 15)) == (2025, "Q3")


def test_latest_on_annual_doc_switches_to_local_newest():
    """用户实际遇到的：在精达 2025 年报上问「最新财报如何」，本地有 2026 三季报 → 切过去。"""
    out = qplan("最新财报如何", cur_year="2025", cur_period="FY")
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "q3"


def test_latest_when_already_on_it_stays():
    assert qplan("最新财报如何") == {"action": "current"}


def test_latest_imports_when_local_is_stale():
    out = qplan("茅台最新财报", company="茅台")
    assert out == {"action": "import", "company": "茅台", "year": 2026, "period": "Q3"}


def test_latest_tries_early_disclosure_first():
    """10 月中旬：三季度已结束但没到截止日，先试 2026 三季报，没有再用半年报。"""
    tgt = with_period({"company": "茅台", "year": None, "multiple": False}, "最新财报",
                      date(2026, 10, 15))
    assert (tgt["year"], tgt["period"], tgt["newer"]) == (2026, "H1", [2026, "Q3"])
    out = plan_resolution(tgt, current_company="精达股份", current_year="2026",
                          current_period="Q3", docs=QDOCS, today=date(2026, 10, 15))
    assert out == {"action": "import", "company": "茅台", "year": 2026, "period": "H1",
                   "newer": [2026, "Q3"]}
    # 11 月初：年报的报告期还没结束，不白查一次
    assert "newer" not in with_period({"company": "", "year": None}, "最新财报", TODAY)


def test_latest_local_early_copy_counts():
    """本地已经有提前披露的那份（比已过截止日的更新），它就是最新。"""
    tgt = with_period({"company": "", "year": None}, "最新财报", date(2026, 10, 15))
    out = plan_resolution(tgt, current_company="精达股份", current_year="2025",
                          current_period="FY", docs=QDOCS, today=date(2026, 10, 15))
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "q3"


def test_explicit_year_beats_latest():
    tgt = with_period({"company": "", "year": 2024, "multiple": False}, "2024年最新财报", TODAY)
    assert "latest" not in tgt


def test_latest_annual_picks_annual_only():
    out = qplan("最新年报怎么样")
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "fy25"
