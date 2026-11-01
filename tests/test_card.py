"""结构化数据 → 指标卡。比率在这里由 Python 算，所以每个数都核对到具体值。"""
import pytest

from ui.card import build_card, wants_card


def _fin(**overrides):
    """精达股份 2025 的真实数字（元）。"""
    items = {
        "revenue": {"values": {"2025": 24_673_670_269.16, "2024": 22_322_579_930.98,
                               "2023": 17_905_865_235.94}, "change": 10.53},
        "np_parent": {"values": {"2025": 599_143_119.39, "2024": 561_708_879.75,
                                 "2023": 426_452_861.87}, "change": 6.66},
        "np_deducted": {"values": {"2025": 569_634_073.52, "2024": 511_634_012.50,
                                   "2023": 398_281_657.00}, "change": 11.34},
        "ocf": {"values": {"2025": 2_475_082_817.33, "2024": -589_153_155.66,
                           "2023": 438_962_688.40}, "change": 520.11},
    }
    items.update(overrides)
    return {"version": 1, "unit": "元", "page": 7, "years": ["2025", "2024", "2023"],
            "items": items}


def _kpi(card, label):
    return next(k for k in card["kpis"] if k["k"] == label)


def test_six_kpis_with_disclosed_changes_and_computed_ratios():
    card = build_card(_fin())
    assert [k["k"] for k in card["kpis"]] == [
        "营业收入", "归母净利润", "扣非净利润", "经营现金流净额", "归母净利率", "净现比"]
    rev = _kpi(card, "营业收入")
    assert rev == {"k": "营业收入", "v": "246.74", "u": "亿元", "d": "10.53%", "up": True}
    # 归母净利率 5.99/246.74 = 2.43%，上年 5.62/223.23 = 2.52%，下降 0.09pct
    margin = _kpi(card, "归母净利率")
    assert margin["v"] == "2.43" and margin["u"] == "%"
    assert margin["d"] == "0.09pct" and margin["up"] is False
    # 净现比 24.75/5.99 = 4.13 倍；上年经营现金流为负、比值 -1.05，和它比没有意义 → 不出变化
    cash = _kpi(card, "净现比")
    assert cash["v"] == "4.13" and cash["u"] == "倍"
    assert "d" not in cash


def test_cash_ratio_change_when_both_years_are_positive():
    """时代新材：21.82/5.14 = 4.25 倍，上年 11.42/4.45 = 2.57 倍，上升 1.68 倍。"""
    fin = _fin(
        np_parent={"values": {"2025": 513_614_400, "2024": 444_861_200, "2023": 386_157_700},
                   "change": 15.45},
        ocf={"values": {"2025": 2_182_147_000, "2024": 1_142_376_400, "2023": 795_428_500},
             "change": 91.02},
    )
    cash = _kpi(build_card(fin), "净现比")
    assert cash["v"] == "4.25" and cash["d"] == "1.68倍" and cash["up"] is True


def test_charts_are_old_to_new_and_keep_negative_values():
    card = build_card(_fin())
    rev, np_ = card["charts"]
    assert rev["labels"] == ["2023", "2024", "2025"]
    assert rev["values"] == [179.06, 223.23, 246.74]
    assert rev["range"] == "2023–2025" and rev["unit"] == "亿元"
    assert rev["yoy"][0] == "—" and rev["yoy"][2] == "+10.53%"
    assert np_["values"] == [4.26, 5.62, 5.99]


def test_card_note_names_the_source_page_not_demo():
    card = build_card(_fin())
    assert card["cardNote"] == "程序抽取 · 年报第 7 页 · 已与披露增减核对"
    assert "演示" not in card["cardNote"]
    assert card["cardTitle"] == "主要会计数据 · 2025"


def test_small_company_switches_to_ten_thousand_yuan():
    tiny = {"values": {"2025": 9_615_292.10, "2024": 73_469_582.94, "2023": 1.0}, "change": -86.91}
    card = build_card(_fin(np_deducted=tiny))
    k = _kpi(card, "扣非净利润")
    assert k["v"] == "961.53" and k["u"] == "万元"
    assert k["d"] == "86.91%" and k["up"] is False


def test_cash_ratio_is_skipped_when_net_profit_is_not_positive():
    """净利润为负时，经营现金流 ÷ 净利润 负负得正会显得很好看 —— 不出。"""
    loss = {"values": {"2025": -1e8, "2024": 2e8, "2023": 1e8}, "change": -150.0}
    card = build_card(_fin(np_parent=loss))
    assert "净现比" not in [k["k"] for k in card["kpis"]]


def test_missing_items_are_left_out_and_empty_data_gives_no_card():
    fin = _fin()
    del fin["items"]["ocf"]
    card = build_card(fin)
    labels = [k["k"] for k in card["kpis"]]
    assert "经营现金流净额" not in labels and "净现比" not in labels
    assert build_card(None) is None
    assert build_card({"version": 1, "items": {}}) is None


@pytest.mark.parametrize("q", [
    "营业收入和归母净利润分别是多少？",
    "七匹狼利润率变化如何？",
    "这家公司经营情况怎么样",
    "经营活动现金流为什么大幅增加",
    "近三年业绩趋势",
])
def test_wants_card_for_financial_questions(q):
    assert wants_card(q)


@pytest.mark.parametrize("q", ["那2024年呢？", "去年呢", "那么2023年怎么样"])
def test_short_followup_inherits_the_previous_topic(q):
    """「那2024年呢」本身不带话题：上一问在问利润，这一问也该出（那一年的）卡。"""
    assert wants_card(q, previous=["七匹狼利润率变化如何？"])
    assert not wants_card(q, previous=["前五大客户集中度如何？"])


def test_chained_followups_keep_the_original_topic():
    """「利润率怎么变」→「那2024年呢」→「那2023年呢」：第三问仍然在问利润。"""
    assert wants_card("那2023年呢？", previous=["七匹狼利润率变化如何？", "那2024年呢？"])
    assert not wants_card("那2023年呢？", previous=["前五大客户集中度如何？", "那2024年呢？"])


@pytest.mark.parametrize("q", ["那2024年呢？", "去年呢"])
def test_bare_followup_without_context_gets_no_card(q):
    assert not wants_card(q)


def test_long_question_does_not_inherit():
    """只有短追问才继承话题；完整的新问题按自己的内容判断。"""
    assert not wants_card("公司今年在海外开了哪些新门店，进展如何？", previous=["营业收入是多少？"])


@pytest.mark.parametrize("q", [
    "前五大客户与供应商集中度如何？",
    "研发投入的金额与资本化比例？",
    "公司有哪些重大诉讼",
    "董事会成员有哪些人",
    "公司面临哪些主要风险",
])
def test_no_card_for_other_questions(q):
    assert not wants_card(q)
