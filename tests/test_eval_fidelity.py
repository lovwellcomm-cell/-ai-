"""数字可溯源性检查的测试。

这套检查是测量的地基：它必须**只在真的有把握时说"有出处"**，否则又会变成
一个骗自己的指标（这个项目上已经栽过一次）。所以下面每一种情况都钉住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eval_fidelity import check, extract_numbers  # noqa: E402

# 一份假的"片段"，数字取自真实年报的写法
CHUNK = {"text": "营业收入 24,673,670,269.16 22,322,579,930.98\n"
                 "营业成本 22,489,950,220.41 20,302,000,000.00\n"
                 "经营活动产生的现金流量净额 2,475,082,817.33\n"
                 "净利润 622,525,281.91\n"
                 "营业收入同比增长 10.64%\n营业成本同比增长 10.80%"}
HITS = [CHUNK]


def test_extract_keeps_unit_and_precision():
    n = extract_numbers("即 246.74 亿元")[0]
    assert n["unit"] == "亿元" and n["value"] == 246.74e8 and n["dec"] == 2
    assert extract_numbers("同比 10.64%")[0]["kind"] == "percent"
    assert extract_numbers("2025年")[0]["kind"] == "year"


def test_year_is_not_treated_as_an_amount():
    """「2025年」不是财务数字，不能被算进金额里，否则分母虚高。"""
    res = check("2025年营业收入 24,673,670,269.16 元", HITS)
    assert res["金额个数"] == 1


def test_direct_match():
    res = check("营业收入 24,673,670,269.16 元。", HITS)
    assert res["有出处"] == 1 and res["无出处"] == 0


def test_unit_conversion_matches_the_raw_value():
    """回答写「246.74 亿元」，片段里是 24,673,670,269.16 元 —— 必须能对上。"""
    res = check("营业收入 246.74 亿元。", HITS)
    assert res["有出处"] == 1, res


def test_tolerance_follows_displayed_precision():
    """246.74 亿 与 24,673,670,269.16 元 差 0.03 亿 —— 那只是四舍五入，不是错。"""
    assert check("营业收入 246.74 亿元。", HITS)["无出处"] == 0
    # 但差得多就必须报出来
    assert check("营业收入 300.00 亿元。", HITS)["无出处"] == 1


def test_difference_is_accepted_as_derived():
    """变化额是提示词要求算的，等于两个有出处的数相减。"""
    diff = 24673670269.16 - 22322579930.98
    res = check(f"营业收入增加 {diff:,.2f} 元。", HITS)
    assert res["无出处"] == 0 and res["可推导"] == 1


def test_ratio_tolerance_uses_display_precision():
    """「约为净利润的 3.98 倍」真值 3.9759，只差 0.004。

    反解除数时误差会放大：目标容差只有 5e-5，但算出来的除数容差有几千。
    早先共用一个容差，这条会被误判成无出处。
    """
    res = check("经营活动现金流量净额约为净利润的 3.9759 倍。", HITS)
    assert res["无出处"] == 0, res["无出处样例"]


def test_prior_period_value_from_growth_rate():
    """「上期约 202.98 亿」= 本期 224.90 亿 ÷ (1+10.80%)，只有这种算法对得上。

    增长率必须出现在回答里才会被采用 —— 拿片段里任意百分比去反推会大幅削弱
    「抓编造」的能力，所以刻意收紧到只用回答自己声称的增长率。
    """
    res = check("主营业务成本同比增长 10.80%。按片段数字计算，成本上期约 202.98 亿元。", HITS)
    assert res["无出处"] == 0 and res["可推导"] == 1, res["无出处样例"]


def test_second_round_derivation():
    """增加额 = 本期 − 上期，而上期本身就是推导值。"""
    res = check("同比增长 10.80%。成本上期约 202.98 亿元，增加约 21.92 亿元。", HITS)
    assert res["无出处"] == 0, res["无出处样例"]


def test_fabricated_number_is_reported():
    """核心能力：凭空出现的数字必须被抓住。"""
    res = check("营业收入 999.99 亿元。", HITS)
    assert res["无出处"] == 1 and "999.99" in res["无出处样例"][0]


def test_fabricated_number_is_not_rescued_by_arithmetic():
    """编一个"看起来像算出来的"数字，同样不能放行。"""
    res = check("营业收入增加 888,888,888.88 元。", HITS)
    assert res["无出处"] == 1


def test_zero_in_the_chunks_cannot_launder_anything():
    """回归：片段里大量存在「0.00」。

    曾经的写法里 0/target = 0 会和池子里的 0 匹配上，容差还跟着塌成 0 ——
    实测任何编造的数字都能靠这一条蒙混过关。
    """
    hits = [{"text": "项目 0.00 0.00\n营业收入 24,673,670,269.16"}]
    assert check("净利润 7,777,777,777.77 元。", hits)["无出处"] == 1


def test_impossible_percentage_share_is_not_accepted():
    """回归：a × b% = target 里 b 必须落在 0-100。

    不加这道门会解出 416% 这种不可能的值，算术上却成立，
    于是「999.99 亿元」这种大额也能蒙混过去。
    """
    hits = [{"text": "营业收入 24,673,670,269.16\n成本 22,489,950,220.41"}]
    assert check("营业收入 999.99 亿元。", hits)["无出处"] == 1


def test_fabrication_detection_on_a_realistic_chunk_pool():
    """真实检索只给 8 个片段、约一百个数字，大额小额都能抓住。

    注意池子密度会影响小数的可验证性：把整份年报（8000+ 个数字）当池子时，
    「9.99」这种小数几乎必然能由某两个数的差凑出来，就验不出来了。
    实际用法是拿**检索到的片段**当池子，所以不受这个影响。
    """
    assert check("营业收入 8,888,888,888.88 元。", HITS)["无出处"] == 1
    assert check("每股收益 9.99 元。", HITS)["无出处"] == 1
    assert check("研发投入 3.33 亿元。", HITS)["无出处"] == 1


def test_unsourced_percentage_is_reported():
    res = check("同比增长 77.77%。", HITS)
    assert res["无出处百分比"] == 1
    assert check("同比增长 10.64%。", HITS)["无出处百分比"] == 0


def test_table_unit_declaration_is_honoured():
    """「单位：万元」的表，裸数字要按声明换算。

    实测踩过：时代新材的「主要会计数据」整张表以万元计，模型写成
    「214.71亿元（2,147,054.45万元）」完全正确，但检查器把 2,147,054.45 当成**元**
    存进池子，于是正确换算被整片判成编造。

    这个 bug 只在换一份年报时才暴露 —— 内置示例年报的报表以元为单位，
    裸数字正好等于答案里的原值。
    """
    hits = [{"text": "单位：万元 币种：人民币\n主要会计数据 2025年 2024年\n"
                     "营业收入 2,147,054.45 2,005,500.74"}]
    assert check("营业收入 214.71亿元（2,147,054.45万元）。", hits)["无出处"] == 0
    assert check("营业收入 200.55亿元。", hits)["无出处"] == 0
    # 编造的数字仍然要抓住
    assert check("营业收入 999.99亿元。", hits)["无出处"] == 1


def test_unit_declaration_can_come_from_the_context_header():
    """表头是检索层补上去的（缺年份表头的报表片段），量纲声明可能在它里面。"""
    hits = [{"text": "营业收入 2,147,054.45", "context_header": "单位：万元"}]
    assert check("营业收入 214.71亿元。", hits)["无出处"] == 0


def test_no_hits_means_nothing_is_sourced():
    """没有片段时不能因为"没有反例"就判为有出处。"""
    res = check("营业收入 24,673,670,269.16 元。", [])
    assert res["有出处"] == 0 and res["无出处"] == 1


def test_empty_answer_is_safe():
    res = check("", HITS)
    assert res["金额个数"] == 0 and res["无出处"] == 0
