"""季报 / 半年报的「主要会计数据」抽取和指标卡（rag/financials.py 后半部分、ui/card.py）。

夹具是真实季报、半年报的原文（pdfium 拍平后的行），每份代表一种格式：
  · 精达 2026 三季报：上交所，单季 + 年初至今四格，单季经营现金流「不适用」，科目折三行
  · 七匹狼 2026 三季报：深交所，增减带 %，占位符「—」，归母净利润为负
  · 七匹狼 2026 一季报：深交所，三格（本期、上年同期、增减）
  · 精达 2026 半年报：扣非科目被分页截断，后半截在页眉页码之后
  · 时代新材 2026 半年报：单位「万元」
"""
from rag.financials import audit_status, extract_periodic_from_lines, parse_periodic_rows
from ui.card import build_card


def _lines(text: str) -> list[str]:
    return text.strip("\n").split("\n")


JD_Q3_COVER = """
铜陵精达特种电磁线股份有限公司 2026 年第三季度报告
第三季度财务报表是否经审计
□是 √否
一、主要财务数据
（一） 主要会计数据和财务指标
单位：元 币种：人民币
项目 本报告期
本报告期比
上年同期增
减变动幅度
(%)
年初至报告期末
年初至报告期
末比上年同期
增减变动幅度
(%)
营业收入 8,129,474,578.52 37.31 23,430,538,137.40 31.81
利润总额 238,572,529.41 20.43 668,635,213.61 10.66
归属于上市公司股东的
净利润
173,071,905.30 19.75 497,864,679.34 10.42
归属于上市公司股东的
扣除非经常性损益的净
利润
161,864,524.37 20.64 481,267,021.02 13.46
经营活动产生的现金流
量净额
不适用 不适用 460,169,804.85 -67.73
"""
JD_Q3_PAGE2 = """
铜陵精达特种电磁线股份有限公司 2026 年第三季度报告
2 / 11
基本每股收益（元/股） 0.08 14.29 0.23 9.52
稀释每股收益（元/股） 0.08 14.29 0.23 9.52
本报告期末 上年度末
本报告期末比
上年度末增减
变动幅度(%)
总资产 17,406,915,175.45 14,467,092,270.19 20.32
归属于上市公司股东的
所有者权益
6,694,271,851.33 6,149,606,753.81 8.86
注：“本报告期”指本季度初至本季度末 3 个月期间，下同。
（二） 非经常性损益项目和金额
减：所得税影响额 1,890,132.67 5,306,746.93
少数股东权益影响额（税后） 172,747.11 692,572.10
合计 11,207,380.93 16,597,658.32
"""
JD_Q3_STATEMENTS = """
一、营业总收入 23,430,538,137.40 17,776,337,532.97
其中：营业收入 23,430,538,137.40 17,776,337,532.97
四、利润总额（亏损总额以“-”号填列） 668,635,213.61 604,198,725.73
1.归属于母公司股东的净利润（净亏损以“-”号填列） 497,864,679.34 450,880,616.26
经营活动产生的现金流量净额 460,169,804.85 1,425,905,910.24
"""


def _jd_q3():
    return [_lines(JD_Q3_COVER), _lines(JD_Q3_PAGE2), _lines(JD_Q3_STATEMENTS)]


def test_q3_ytd_is_verified_against_statements():
    """三季报表里没有上年同期金额：用利润表/现金流量表里的上年同期复算增减。"""
    fin = extract_periodic_from_lines(_jd_q3(), "Q3", 2026)
    items = fin["items"]
    assert fin["period"] == "Q3" and fin["years"] == ["2026", "2025"] and fin["page"] == 1
    assert items["revenue"]["values"] == {"2026": 23430538137.40, "2025": 17776337532.97}
    assert items["revenue"]["change"] == 31.81
    assert items["np_parent"]["values"]["2025"] == 450880616.26
    assert items["ocf"]["values"]["2025"] == 1425905910.24
    assert items["total_profit"]["values"]["2025"] == 604198725.73
    # 单季：第一格
    assert items["revenue"]["quarter"] == {"value": 8129474578.52, "change": 37.31}
    assert "quarter" not in items["ocf"]                    # 单季经营现金流「不适用」
    # 扣非用「归母净利润 − 非经常性损益合计」核对：497,864,679.34 − 16,597,658.32
    assert items["np_deducted"]["values"] == {"2026": 481267021.02}
    # 资产类：三个数，季报写「所有者权益」
    assert items["equity_parent"]["values"] == {"2026": 6694271851.33, "2025": 6149606753.81}
    assert fin["audited"] is False


def test_q3_rows_without_statement_match_are_dropped():
    """报表里找不到对得上的上年同期 —— 不展示，也不拿增减倒推一个上年金额。"""
    broken = _lines(JD_Q3_STATEMENTS.replace("450,880,616.26", "400,000,000.00"))
    fin = extract_periodic_from_lines([_lines(JD_Q3_COVER), _lines(JD_Q3_PAGE2), broken],
                                      "Q3", 2026)
    assert fin is None                      # 没有归母净利润，整张卡不出


def test_q3_statement_row_wrapped_onto_two_lines():
    """精达 2025 三季报的利润表：归母净利润科目名太长，本期、上年同期金额各占一行。"""
    wrapped = _lines(JD_Q3_STATEMENTS.replace(
        "1.归属于母公司股东的净利润（净亏损以“-”号填列） 497,864,679.34 450,880,616.26",
        "1.归属于母公司股东的净利润（净亏损以“-”号填\n列）\n497,864,679.34\n450,880,616.26"))
    fin = extract_periodic_from_lines([_lines(JD_Q3_COVER), _lines(JD_Q3_PAGE2), wrapped], "Q3", 2026)
    assert fin["items"]["np_parent"]["values"]["2025"] == 450880616.26


def test_q3_deducted_dropped_when_nonrecurring_total_disagrees():
    page2 = _lines(JD_Q3_PAGE2.replace("16,597,658.32", "26,597,658.32"))
    fin = extract_periodic_from_lines([_lines(JD_Q3_COVER), page2, _lines(JD_Q3_STATEMENTS)],
                                      "Q3", 2026)
    assert "np_deducted" not in fin["items"] and "np_parent" in fin["items"]


QPL_Q3 = """
3.第三季度财务会计报告是否经过审计
□是 否
（一）主要会计数据和财务指标
公司是否需追溯调整或重述以前年度会计数据
□是 否
本报告期
本报告期比上年同
期增减
年初至报告期末
年初至报告期末
比上年同期增减
营业收入（元） 611,462,915.80 -6.01% 2,026,243,547.09 0.05%
归属于上市公司股东的净利润（元） -23,251,272.32 -109.59% -50,553,323.89 -112.55%
归属于上市公司股东的扣除非经常性损益
的净利润（元）
-61,308,571.09 -232.74% 81,934,046.37 667.10%
经营活动产生的现金流量净额（元） — — 87,274,318.74 380.30%
基本每股收益（元/股） -0.03 -108.33% -0.08 -113.56%
本报告期末 上年度末 本报告期末比上年度末增减
总资产（元） 9,364,138,667.07 10,060,538,983.53 -6.92%
归属于上市公司股东的所有者权益（元） 6,620,551,800.82 6,744,880,636.50 -1.84%
（二）非经常性损益项目和金额
合计 38,057,298.77 -132,487,370.26 -- 其他符合非经常性损益定义的损益项目的具体情况：
"""
QPL_Q3_STATEMENTS = """
其中：营业收入 2,026,243,547.09 2,025,137,986.99
1.归属于母公司股东的净利润（净亏损以“-”号填列） -50,553,323.89 402,841,036.23
经营活动产生的现金流量净额 87,274,318.74 -31,136,577.90
"""


def test_szse_q3_with_dash_placeholders_and_losses():
    fin = extract_periodic_from_lines([_lines(QPL_Q3), _lines(QPL_Q3_STATEMENTS)], "Q3", 2026)
    items = fin["items"]
    assert items["np_parent"]["values"] == {"2026": -50553323.89, "2025": 402841036.23}
    assert items["np_parent"]["quarter"]["value"] == -23251272.32
    assert items["ocf"]["values"]["2025"] == -31136577.90 and "quarter" not in items["ocf"]
    assert items["np_deducted"]["values"]["2026"] == 81934046.37     # 合计行核对通过
    assert fin["audited"] is False                                   # 「□是 否」：勾掉的是「否」


QPL_Q1 = """
第一季度财务会计报告是否经过审计
□是 否
（一） 主要会计数据和财务指标
公司是否需追溯调整或重述以前年度会计数据
□是 否
本报告期 上年同期 本报告期比上年同期增减（%）
营业收入（元） 885,376,106.15 844,034,694.74 4.90%
归属于上市公司股东的净利润（元） 49,823,715.57 149,508,166.50 -66.67%
归属于上市公司股东的扣除非经常性损
益的净利润（元）
155,597,501.36 38,645,212.78 302.63%
经营活动产生的现金流量净额（元） 230,546,382.12 108,038,831.45 113.39%
基本每股收益（元/股） 0.07 0.22 -68.18%
本报告期末 上年度末 本报告期末比上年度末增减（%）
总资产（元） 9,781,894,649.49 10,060,538,983.53 -2.77%
归属于上市公司股东的所有者权益
（元）
6,792,612,571.49 6,744,880,636.50 0.71%
（二） 非经常性损益项目和金额
"""


def test_q1_three_columns_are_recomputed():
    fin = extract_periodic_from_lines([_lines(QPL_Q1)], "Q1", 2026)
    items = fin["items"]
    assert items["revenue"]["values"] == {"2026": 885376106.15, "2025": 844034694.74}
    assert items["np_deducted"]["values"]["2025"] == 38645212.78
    assert items["equity_parent"]["values"]["2026"] == 6792612571.49
    assert set(items) == {"revenue", "np_parent", "np_deducted", "ocf",
                          "total_assets", "equity_parent"}


def test_q1_restated_prior_uses_adjusted_column():
    """追溯调整时「上年同期」拆成调整前 / 调整后两列：复算要用调整后。"""
    text = QPL_Q1.replace("885,376,106.15 844,034,694.74 4.90%",
                          "885,376,106.15 840,000,000.00 844,034,694.74 4.90%")
    fin = extract_periodic_from_lines([_lines(text)], "Q1", 2026)
    assert fin["items"]["revenue"]["values"]["2025"] == 844034694.74


def test_q1_wrong_change_is_dropped():
    text = QPL_Q1.replace("230,546,382.12 108,038,831.45 113.39%",
                          "230,546,382.12 108,038,831.45 13.39%")
    fin = extract_periodic_from_lines([_lines(text)], "Q1", 2026)
    assert "ocf" not in fin["items"]


JD_H1 = """
三、 本半年度报告未经审计。
七、 公司主要会计数据和财务指标
(一) 主要会计数据
单位：元 币种：人民币
主要会计数据
本报告期
（1－6月）
上年同期
本报告期比上
年同期增减(%)
营业收入 15,301,063,558.88 11,855,668,677.72 29.06
利润总额 430,062,684.20 406,093,257.88 5.90
归属于上市公司股东的净利润 324,792,774.04 306,349,885.51 6.02
归属于上市公司股东的扣除非经常 319,402,496.65 290,015,545.04 10.13
"""
JD_H1_PAGE6 = """
铜陵精达特种电磁线股份有限公司2026 年半年度报告
6 / 172
性损益的净利润
经营活动产生的现金流量净额 -70,274,330.63 994,996,013.80 -107.06
本报告期末 上年度末
本报告期末比
上年度末增减
(%)
归属于上市公司股东的净资产 6,358,273,583.13 6,149,606,753.81 3.39
总资产 16,775,501,866.88 14,467,092,270.19 15.96
(二) 主要财务指标
"""


def test_label_cut_by_page_break_is_joined():
    """「归属于上市公司股东的扣除非经常 …数字…」+ 页眉 + 页码 +「性损益的净利润」。"""
    pages = [_lines(JD_H1), _lines(JD_H1_PAGE6)]
    fin = extract_periodic_from_lines(pages, "H1", 2026)
    items = fin["items"]
    assert items["np_deducted"]["values"] == {"2026": 319402496.65, "2025": 290015545.04}
    assert items["ocf"]["values"]["2026"] == -70274330.63
    assert len(items) == 7 and fin["audited"] is False and fin["page"] == 1


SD_H1 = """
(一) 主要会计数据
单位：万元 币种：人民币
主要会计数据
本报告期
（1－6月）
上年同期
本报告期比上年
同期增减(%)
营业收入 1,046,745.59 925,606.85 13.09
利润总额 45,320.95 40,478.90 11.96
归属于上市公司股东的净利润 34,590.77 30,322.61 14.08
归属于上市公司股东的扣除非经常性
损益的净利润
31,242.49 23,331.21 33.91
经营活动产生的现金流量净额 -108,768.78 74,390.19 -246.21
(二) 主要财务指标
"""


def test_half_year_in_ten_thousand_yuan():
    fin = extract_periodic_from_lines([_lines(SD_H1)], "H1", 2026)
    assert fin["unit"] == "万元"
    assert round(fin["items"]["revenue"]["values"]["2026"] / 1e8, 2) == 104.67
    assert fin["audited"] is None                   # 读到的页里没写


def test_parse_rows_skips_explanation_lines():
    """变动原因表里「营业收入_本报告期 37.31 原材料价格上涨…」不是数据行。"""
    rows = parse_periodic_rows(["营业收入_本报告期 37.31 原材料价格上涨及销量增加所致。"])
    assert rows == {}


def test_audit_status_variants():
    assert audit_status([["第一季度财务报表是否经审计", "□是 √否"]]) is False
    assert audit_status([["第一季度财务报表是否经审计", "√是 □否"]]) is True
    assert audit_status([["半年度财务报告是否已经审计", "□是 否"]]) is False
    assert audit_status([["无关内容"]]) is None


# ---------------------------------------------------------------- 指标卡

def test_q3_card_shows_ytd_and_single_quarter():
    card = build_card(extract_periodic_from_lines(_jd_q3(), "Q3", 2026))
    assert card["cardTitle"] == "主要会计数据 · 2026 前三季度"
    assert card["cardNote"] == "程序抽取 · 三季报第 1 页 · 已与利润表、现金流量表核对 · 未经审计"
    by = {k["k"]: k for k in card["kpis"]}
    assert list(by) == ["营业收入", "归母净利润", "扣非净利润", "经营现金流净额",
                        "Q3 单季营收", "Q3 单季归母净利"]
    assert by["营业收入"]["v"] == "234.31" and by["营业收入"]["d"] == "31.81%"
    assert by["Q3 单季营收"]["v"] == "81.29" and by["Q3 单季营收"]["d"] == "37.31%"
    assert by["经营现金流净额"]["up"] is False
    chart = card["charts"][0]
    assert chart["title"] == "营业收入（1–9月）"
    assert chart["labels"] == ["2025", "2026"] and chart["values"] == [177.76, 234.31]


def test_half_year_card_keeps_ratios():
    card = build_card(extract_periodic_from_lines([_lines(JD_H1), _lines(JD_H1_PAGE6)], "H1", 2026))
    assert card["cardTitle"] == "主要会计数据 · 2026 上半年"
    assert card["cardNote"] == "程序抽取 · 半年报第 1 页 · 已与披露增减核对 · 未经审计"
    names = [k["k"] for k in card["kpis"]]
    assert "归母净利率" in names
    assert "净现比" in names                       # 归母净利润为正才出
    assert card["charts"][1]["title"] == "归母净利润（1–6月）"


def test_unknown_audit_status_says_usually_unaudited():
    card = build_card(extract_periodic_from_lines([_lines(SD_H1)], "H1", 2026))
    assert card["cardNote"].endswith("通常未经审计")
