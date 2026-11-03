"""多年趋势第二步：最近 8 季的单季序列（rag/quarters.py）和单季折线卡片。

数字是精达股份 2025 年报、2025 三季报、2026 一季报 / 半年报 / 三季报里的真实值（亿元，保留两位），
2026-11-03 从本地报告程序抽取；用 2024 年四季相加 ≈ 2024 年报全年（223.23）核对过。
"""
import pytest

from rag import quarters
from rag.quarters import build, needed_reports, series, wants_quarters
from ui.card import build_card

Y = 1e8


def _q(year, period, rev, np_, quarter=None):
    y0, y1 = str(year), str(year - 1)
    items = {"revenue": {"label": "营业收入", "change": 1.0, "values": {y0: rev[0] * Y, y1: rev[1] * Y}},
             "np_parent": {"label": "归属于上市公司股东的净利润", "change": 1.0,
                           "values": {y0: np_[0] * Y, y1: np_[1] * Y}}}
    if quarter:
        items["revenue"]["quarter"] = {"value": quarter[0] * Y, "change": 1.0}
        items["np_parent"]["quarter"] = {"value": quarter[1] * Y, "change": 1.0}
    return {"period": period, "page": 1, "years": [y0, y1], "items": items}


def _fy(year, rev, np_):
    ys = [str(year - i) for i in range(3)]
    return {"period": "FY", "page": 7, "years": ys,
            "items": {"revenue": {"label": "营业收入", "change": 10.53,
                                  "values": dict(zip(ys, [v * Y for v in rev]))},
                      "np_parent": {"label": "归属于上市公司股东的净利润", "change": 6.66,
                                    "values": dict(zip(ys, [v * Y for v in np_]))}}}


REPORTS = {
    (2026, "Q3"): _q(2026, "Q3", (234.31, 177.76), (4.98, 4.51), quarter=(81.29, 1.73)),
    (2026, "H1"): _q(2026, "H1", (153.01, 118.56), (3.25, 3.06)),
    (2026, "Q1"): _q(2026, "Q1", (70.36, 55.27), (1.37, 1.25)),
    (2025, "FY"): _fy(2025, (246.74, 223.23, 179.06), (5.99, 5.62, 4.26)),
    (2025, "Q3"): _q(2025, "Q3", (177.76, 160.87), (4.51, 4.16), quarter=(59.21, 1.45)),
}


def test_needed_reports():
    assert needed_reports(2026, "Q3") == [(2026, "Q3"), (2026, "H1"), (2026, "Q1"), (2025, "FY"), (2025, "Q3")]
    assert needed_reports(2026, "Q1") == [(2026, "Q1"), (2025, "FY"), (2025, "Q3"), (2025, "H1"), (2025, "Q1")]
    assert needed_reports(2025, "FY") == [(2025, "FY"), (2025, "Q3"), (2025, "H1"), (2025, "Q1")]


def test_eight_quarters_from_five_reports():
    s = series(list(REPORTS.values()), 2026, "Q3")
    rev = s["items"]["revenue"]
    assert rev["labels"] == ["24Q4", "25Q1", "25Q2", "25Q3", "25Q4", "26Q1", "26Q2", "26Q3"]
    assert [round(v / Y, 2) for v in rev["values"]] == [62.36, 55.27, 63.29, 59.21, 68.98, 70.36, 82.65, 81.29]
    assert s["anchor"] == "2026 三季报"
    assert s["sources"] == ["2025 三季报", "2025 年报", "2026 一季报", "2026 半年报", "2026 三季报"]


def test_disclosed_q3_single_beats_subtraction():
    """第三季度用三季报披露的单季数：(234.31 − 153.01 = 81.30) vs 披露 81.29。"""
    rev = series(list(REPORTS.values()), 2026, "Q3")["items"]["revenue"]
    assert rev["values"][-1] == pytest.approx(81.29 * Y)


def test_own_value_beats_prior_column():
    """2025 年前三季度累计：2025 三季报自己的数优先，不用 2026 三季报的「上年同期」（可能追溯调整过）。"""
    fins = dict(REPORTS)
    fins[(2026, "Q3")] = _q(2026, "Q3", (234.31, 999.0), (4.98, 4.51), quarter=(81.29, 1.73))
    rev = series(list(fins.values()), 2026, "Q3")["items"]["revenue"]
    assert round(rev["values"][4] / Y, 2) == 68.98          # 25Q4 = 246.74 − 177.76


def test_gap_keeps_only_latest_run_and_short_run_is_dropped():
    fins = [f for k, f in REPORTS.items() if k != (2025, "Q3")]
    s = series(fins, 2026, "Q3")
    # 没有 2025 三季报：2025Q3 用 2026 三季报的上年同期累计照样算得出，只缺 2024Q4
    assert s["items"]["revenue"]["labels"][0] == "25Q1"
    only_q3 = [REPORTS[(2026, "Q3")], REPORTS[(2026, "H1")]]
    assert series(only_q3, 2026, "Q3") is None              # 只有 4 季，不画


def test_build_respects_budget_and_survives_errors():
    calls = []

    def load(company, y, p, docs, *, allow_fetch):
        calls.append((y, p, allow_fetch))
        if (y, p) == (2025, "Q3"):
            raise RuntimeError("下载超时")
        local = (y, p) in ((2026, "Q3"), (2026, "H1"))
        fin = REPORTS[(y, p)] if local or allow_fetch else None
        return fin, (not local and allow_fetch)

    s, fetched, errors = build("精达股份", 2026, "Q3", [], budget=1, load=load)
    assert fetched == 1 and errors == ["2025 三季报：RuntimeError: 下载超时"]
    # 额度 1 份：2026 一季报下载后，2025 年报只能看缓存（这里没有）→ 只剩 26Q1–26Q3，不够画
    assert [c[2] for c in calls] == [True, True, True, False, False] and s is None


def test_card_draws_quarter_lines():
    s = series(list(REPORTS.values()), 2026, "Q3")
    card = build_card(REPORTS[(2026, "Q3")], quarters=s)
    rev, np_ = card["charts"]
    assert rev["kind"] == "line" and rev["title"] == "单季营业收入" and rev["range"] == "24Q4–26Q3"
    assert rev["values"][-1] == 81.29
    # 同比和四个季度前比：26Q3 81.29 vs 25Q3 59.21
    assert rev["yoy"][:4] == ["—"] * 4 and rev["yoy"][-1] == "+37.29%"
    assert "cagr" not in rev and np_["title"] == "单季归母净利润"
    assert card["cardTitle"] == "主要会计数据 · 2026 前三季度 · 近 8 季单季"
    assert card["cardNote"].endswith("单季据2025 三季报、2025 年报、2026 一季报、2026 半年报、2026 三季报的累计数相减"
                                     "（第三季度用三季报披露的单季数）")
    # 指标卡不变：还是年初至今 + 第三季度单季
    assert [k["k"] for k in card["kpis"]][:2] == ["营业收入", "归母净利润"]


def test_card_without_series_unchanged():
    assert build_card(REPORTS[(2026, "Q3")], quarters=None) == build_card(REPORTS[(2026, "Q3")])


@pytest.mark.parametrize("q", ["最近8个季度营收走势", "单季利润怎么样", "近几个季度净利润", "分季度看营收"])
def test_quarter_questions(q):
    assert wants_quarters(q)


@pytest.mark.parametrize("q", ["2025年营收多少", "近几年营收趋势", "第三季度营收多少", "前三季度利润"])
def test_not_quarter_questions(q):
    assert not wants_quarters(q)


def test_quarter_followup_inherits():
    assert wants_quarters("那净利润呢", previous=["最近几个季度营收"])
    assert not wants_quarters("近几年营收趋势如何", previous=["最近几个季度营收和净利润走势如何"])
    assert quarters.N_QUARTERS == 8


def test_series_hit_for_answer():
    s = series(list(REPORTS.values()), 2026, "Q3")
    hit = quarters.as_hit(s, "jd26q3", 1, "精达股份")
    lines = hit["text"].splitlines()
    assert lines[0].startswith("以下单季数由程序据2025 三季报、2025 年报") and "未经公司单独披露" in lines[0]
    assert lines[1].startswith("营业收入（单季）：2024 年第四季度 62.36 亿元；2025 年第一季度 55.27 亿元")
    assert lines[1].endswith("2026 年第三季度 81.29 亿元")
    assert hit["section"] == "单季数据 · 最近 8 季" and hit["note_src"] == "精达股份 2026 三季报"
    assert hit["derived"] and hit["note"] and hit["page_start"] == 1
    assert quarters.as_hit(None, "jd26q3", 1, "精达股份") is None


def test_series_hit_replaced_not_deduped_by_page():
    s = series(list(REPORTS.values()), 2026, "Q3")
    hit = quarters.as_hit(s, "jd26q3", 1, "精达股份")
    table = {"doc_id": "jd26q3", "page_start": 1, "text": "主要会计数据"}
    assert quarters.attach([table, dict(hit, text="旧")], hit) == [table, hit]


def test_derived_numbers_not_noted_or_used_as_caliber_facts():
    from rag.caliber import note_lines
    from rag.notes import from_answer

    hit = quarters.as_hit(series(list(REPORTS.values()), 2026, "Q3"), "jd26q3", 1, "精达股份")
    answer = "| 季度 | 营业收入 |\n|---|---|\n| 2024 年第四季度 | 62.36 亿元 [1] |\n"
    assert from_answer(answer, [hit], [], "jd26q3", "精达股份 2026 三季报") == []
    assert note_lines([hit]) == []
