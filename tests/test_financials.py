"""「主要会计数据」结构化抽取。

夹具是四份真实年报里这张表的**原文**（pdfium 拍平后的行），每份代表一种踩过的格式。
抽错的代价是在投资者面前摆一个错数字，所以每条用例都核对到具体金额。
"""
import json

import pytest

from rag import config, financials
from rag.financials import extract_from_lines, parse_rows, verify


def _lines(text: str) -> list[str]:
    return text.strip("\n").split("\n")


# 精达股份 2025：上交所格式、单位「元」、扣非科目折行且数字单独成行、上年经营现金流为负
JINGDA = """
七、 近三年主要会计数据和财务指标
(一) 主要会计数据
单位：元 币种：人民币
主要会计数据 2025年 2024年
本期比上年同期
增减(%) 2023年
营业收入 24,673,670,269.16 22,322,579,930.98 10.53 17,905,865,235.94
利润总额 806,125,138.82 736,916,287.61 9.39 560,952,476.71
归属于上市公司股东的净利润 599,143,119.39 561,708,879.75 6.66 426,452,861.87
归属于上市公司股东的扣除非
经常性损益的净利润
569,634,073.52 511,634,012.50 11.34 398,281,657.00
经营活动产生的现金流量净额 2,475,082,817.33 -589,153,155.66 520.11 438,962,688.40
2025年末 2024年末
本期末比上年同
期末增减（%）
2023年末
归属于上市公司股东的净资产 6,149,606,753.81 5,759,381,112.35 6.78 5,270,540,279.88
总资产 14,467,092,270.19 12,626,655,985.48 14.58 11,312,730,730.79
(二) 主要财务指标
主要财务指标 2025年 2024年
本期比上年
同期增减(%) 2023年
基本每股收益（元／股） 0.28 0.27 3.7 0.21
"""

# 时代新材 2025：这张表是「万元」（而它的报表是「元」）
TIMES = """
七、 近三年主要会计数据和财务指标
(一) 主要会计数据
单位：万元 币种：人民币
主要会计数据 2025年 2024年
本期比上年
同期增减(%) 2023年
营业收入 2,147,054.45 2,005,500.74 7.06 1,753,786.71
利润总额 63,601.48 52,093.01 22.09 39,915.47
归属于上市公司股东的净利润 51,361.44 44,486.12 15.45 38,615.77
归属于上市公司股东的扣除非经
常性损益的净利润
41,107.60 41,082.58 0.06 42,120.79
经营活动产生的现金流量净额 218,214.70 114,237.64 91.02 79,542.85
"""

# 七匹狼 2025：深交所格式，增减带 %，单位写在科目里，科目后半截与数字同行
SEPTWOLVES = """
六、主要会计数据和财务指标
公司是否需追溯调整或重述以前年度会计数据
□是 否
2025 年 2024 年 本年比上年增减 2023 年
营业收入（元） 3,003,522,655.39 3,140,081,720.82 -4.35% 3,444,735,095.51
归属于上市公司股东的净利润（元） 332,653,874.74 284,548,796.80 16.91% 270,111,122.67
归属于上市公司股东的扣除非经常性
损益的净利润（元） 9,615,292.10 73,469,582.94 -86.91% 187,719,528.68
经营活动产生的现金流量净额（元） 394,017,094.99 245,619,722.07 60.42% 385,055,343.59
基本每股收益（元/股） 0.49 0.41 19.51% 0.38
加权平均净资产收益率 5.02% 4.38% 0.64% 4.28%
2025 年末 2024 年末 本年末比上年末增减 2023 年末
总资产（元） 10,060,538,983.53 10,805,526,644.90 -6.89% 10,876,273,248.06
归属于上市公司股东的净资产（元） 6,744,880,636.50 6,571,306,805.58 2.64% 6,428,582,152.05
七、境内外会计准则下会计数据差异
"""

# 贵州茅台 2024：没有单位行（被页眉过滤删掉的那种情况）、增减表头拆成 4 行、
# 科目折在很短的地方（「…股东的净」/「利润 …」）、股本只有 3 个数
MOUTAI = """
七、 近三年主要会计数据和财务指标
(一) 主要会计数据
主要会计数据 2024年 2023年
本期比
上年同
期增减
(%)
2022年
营业收入 170,899,152,276.34 147,693,604,994.14 15.71 124,099,843,771.99
归属于上市公司股东的净
利润 86,228,146,421.62 74,734,071,550.75 15.38 62,717,467,870.12
归属于上市公司股东的扣
除非经常性损益的净利润 86,240,905,977.42 74,752,564,425.52 15.37 62,792,896,829.57
经营活动产生的现金流量
净额 92,463,692,168.43 66,593,247,721.09 38.85 36,698,595,830.03
2024年末 2023年末
本期末
比上年
同期末
增减（%
）
2022年末
归属于上市公司股东的净
资产 233,105,984,399.47 215,668,571,607.43 8.09 197,480,041,239.46
总资产 298,944,579,918.70 272,699,660,092.25 9.62 254,500,826,096.02
股本 1,256,197,800.00 1,256,197,800.00 1,256,197,800.00
"""


def _extract(text: str, page_lines_before: int = 0) -> dict:
    fin = extract_from_lines([["目录"] * page_lines_before, _lines(text)])
    assert fin is not None
    return fin


def test_shanghai_format_with_wrapped_label_and_negative_prior():
    fin = _extract(JINGDA)
    assert fin["unit"] == "元"
    assert fin["page"] == 2
    assert fin["years"] == ["2025", "2024", "2023"]
    it = fin["items"]
    assert set(it) == set(financials.ITEMS)
    assert it["revenue"]["values"]["2025"] == pytest.approx(24_673_670_269.16)
    assert it["np_deducted"]["values"]["2024"] == pytest.approx(511_634_012.50)
    # 上年为负、增减 520.11% —— 按「÷|上年|」复算对得上，负号必须保留
    assert it["ocf"]["values"]["2024"] == pytest.approx(-589_153_155.66)
    assert it["ocf"]["change"] == pytest.approx(520.11)
    assert it["total_assets"]["values"]["2023"] == pytest.approx(11_312_730_730.79)


def test_stops_before_main_financial_indicators():
    """「(二) 主要财务指标」里的每股收益也是 4 个数，不能混进来。"""
    rows = parse_rows(financials._cut_at_end(_lines(JINGDA)))
    assert "基本每股收益" not in str(rows)
    assert len(rows) == 7


def test_ten_thousand_yuan_unit_is_scaled():
    """时代新材的营收 2,147,054.45 万元 = 214.71 亿元，不是 214 万。"""
    fin = _extract(TIMES)
    assert fin["unit"] == "万元"
    assert fin["items"]["revenue"]["values"]["2025"] == pytest.approx(2_147_054.45e4)


def test_shenzhen_format_percent_change_and_unit_in_label():
    fin = _extract(SEPTWOLVES)
    it = fin["items"]
    assert fin["unit"] == "元"
    assert fin["years"] == ["2025", "2024", "2023"]
    assert it["revenue"]["change"] == pytest.approx(-4.35)
    # 科目后半截「损益的净利润（元）」和数字在同一行
    assert it["np_deducted"]["values"]["2025"] == pytest.approx(9_615_292.10)
    assert it["equity_parent"]["values"]["2023"] == pytest.approx(6_428_582_152.05)
    # 深交所这张表没有利润总额 —— 缺就是缺，不能从别处凑
    assert "total_profit" not in it


def test_missing_unit_line_and_short_label_wraps():
    fin = _extract(MOUTAI)
    it = fin["items"]
    assert fin["unit"] == "元"
    assert fin["years"] == ["2024", "2023", "2022"]
    assert it["np_parent"]["values"]["2024"] == pytest.approx(86_228_146_421.62)
    assert it["ocf"]["values"]["2022"] == pytest.approx(36_698_595_830.03)
    assert it["equity_parent"]["values"]["2024"] == pytest.approx(233_105_984_399.47)
    assert "股本" not in json.dumps(fin, ensure_ascii=False)


# 恒瑞医药 2025：扣非科目折成三行，数字单独成行
HENGRUI = """
(一) 主要会计数据
单位：元 币种：人民币
主要会计数据 2025年 2024年
本期比上
年同期增
减(%)
2023年
营业收入 31,629,416,193.83 27,984,605,342.06 13.02 22,819,784,741.31
归属于上市公司股东的
净利润
7,711,054,811.98 6,336,527,014.75 21.69 4,302,435,930.05
归属于上市公司股东的
扣除非经常性损益的净
利润
7,413,405,567.05 6,177,966,481.30 20.00 4,141,262,764.05
经营活动产生的现金流
量净额
11,235,378,130.63 7,422,753,038.71 51.36 7,643,665,074.52
"""

# 万科 2025：负数加括号；扣非、总资产换了说法；表被页眉页码截成两段
VANKE = """
（一）主要会计数据和财务指标
单位：元
项目 2025 年 2024 年 本年比上年增减 2023 年
营业收入 233,432,768,960.43 343,176,440,712.96 -31.98% 465,739,076,702.23
归属于上市公司股东的净利润 (88,556,470,495.64) (49,478,429,211.96) -78.98% 12,162,684,368.86
 万科企业股份有限公司 2025 年度报告
8
项目 2025 年 2024 年 本年比上年增减 2023 年
扣除非经常性损益后归属于上
市公司股东的净利润 (85,917,363,120.34) (45,393,719,289.83) -89.27% 9,793,841,193.09
经营活动产生的现金流量净额 (988,124,860.96) 3,799,847,632.35 -126.00% 3,912,323,920.11
项目 2025 年 12 月 31 日 2024 年 12 月 31 日 本年末比上年末增减 2023 年 12 月 31 日
资产总额 1,020,622,831,532.86 1,286,259,859,765.82 -20.65% 1,504,850,172,117.83
归属于上市公司股东的净资产 116,905,224,808.46 202,666,487,973.00 -42.32% 250,784,613,404.38
"""


# 长江电力 2025：上年追溯调整，上年拆成「调整后 调整前」两列，一行 5 个数
CJDL = """
(一) 主要会计数据
单位：元 币种：人民币
主要会计数据 2025年
2024年
本期比上年
同期增减
(%)
2023年
调整后 调整前
营业收入 86,241,940,222.20 84,491,870,566.52 84,491,870,566.52 2.07 78,143,535,736.10
利润总额 41,739,840,157.92 38,862,377,699.64 38,862,377,699.64 7.40 32,424,047,365.38
归属于上市公司股
东的净利润
34,502,809,176.39 32,496,172,808.65 32,496,172,808.65 6.17 27,244,616,815.27
"""


def test_restated_five_column_rows():
    it = _extract(CJDL)["items"]
    assert it["revenue"]["values"]["2024"] == pytest.approx(84_491_870_566.52)
    assert it["revenue"]["values"]["2023"] == pytest.approx(78_143_535_736.10)
    assert it["np_parent"]["values"]["2025"] == pytest.approx(34_502_809_176.39)


def test_restated_picks_the_column_that_reconciles():
    # 调整后 100、调整前 90，披露增减 10%（按调整后算）：只能取调整后
    assert financials._restated([110.0, 100.0, 90.0, 10.0, 80.0]) == [110.0, 100.0, 10.0, 80.0]
    # 调整前在前（蓝色光标的列序）：取第二列
    assert financials._restated([110.0, 90.0, 100.0, 10.0, 70.0, 80.0]) == [110.0, 100.0, 10.0, 80.0]
    # 两种都对不上：不猜
    assert financials._restated([110.0, 90.0, 95.0, 10.0, 80.0]) is None


def test_three_line_label():
    it = _extract(HENGRUI)["items"]
    assert it["np_deducted"]["values"]["2025"] == pytest.approx(7_413_405_567.05)
    assert it["ocf"]["values"]["2023"] == pytest.approx(7_643_665_074.52)


def test_parenthesized_negatives_and_vanke_aliases():
    fin = _extract(VANKE)
    it = fin["items"]
    assert fin["years"] == ["2025", "2024", "2023"]
    assert it["np_parent"]["values"]["2025"] == pytest.approx(-88_556_470_495.64)
    assert it["np_deducted"]["values"]["2024"] == pytest.approx(-45_393_719_289.83)
    assert it["ocf"]["values"]["2025"] == pytest.approx(-988_124_860.96)
    assert it["total_assets"]["values"]["2025"] == pytest.approx(1_020_622_831_532.86)
    assert it["equity_parent"]["values"]["2024"] == pytest.approx(202_666_487_973.00)


def test_row_whose_change_does_not_reconcile_is_dropped():
    """列错位时复算出来的增减和披露值对不上，这一行宁可丢掉。"""
    bad = JINGDA.replace("利润总额 806,125,138.82 736,916,287.61 9.39",
                         "利润总额 736,916,287.61 806,125,138.82 9.39")
    fin = _extract(bad)
    assert "total_profit" not in fin["items"]
    assert "revenue" in fin["items"]


def test_verify_tolerance():
    assert verify([110.0, 100.0, 10.0, 90.0])
    assert verify([110.0, 100.0, 10.05, 90.0])
    assert not verify([110.0, 100.0, 11.0, 90.0])
    assert not verify([110.0, 0.0, 0.0, 90.0])


def test_table_of_contents_hit_is_skipped():
    """目录页也写着「主要会计数据」，但下面没有数字 —— 要继续往后找，而不是返回 None。"""
    toc = ["目录", "七、近三年主要会计数据和财务指标 ........ 6", "第三节 管理层讨论与分析 ........ 9"]
    fin = extract_from_lines([toc, _lines(JINGDA)])
    assert fin is not None and fin["page"] == 2


def test_no_table_or_missing_core_items_returns_none():
    assert extract_from_lines([["第一节 重要提示", "本公司董事会保证年度报告内容真实。"]]) is None
    only_assets = "\n".join(l for l in _lines(JINGDA) if not l.startswith(("营业收入", "归属于上市公司股东的净利润")))
    assert extract_from_lines([_lines(only_assets)]) is None


def test_load_or_build_writes_cache_and_rebuilds_on_version_change(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    doc = tmp_path / "abc"
    doc.mkdir()
    (doc / "source.pdf").write_bytes(b"%PDF-fake")
    calls = []

    def fake_extract(data, **kw):
        calls.append(data)
        return extract_from_lines([_lines(JINGDA)])

    monkeypatch.setattr(financials, "extract", fake_extract)

    fin = financials.load_or_build("abc")
    assert fin["items"]["revenue"]
    assert (doc / "financials.json").exists()
    assert financials.load_or_build("abc")["page"] == fin["page"]
    assert len(calls) == 1                      # 第二次读缓存，不再读 PDF

    stale = json.loads((doc / "financials.json").read_text(encoding="utf-8"))
    stale["version"] = financials.FIN_VERSION - 1
    (doc / "financials.json").write_text(json.dumps(stale), encoding="utf-8")
    financials.load_or_build("abc")
    assert len(calls) == 2                      # 版本不符就重建


def test_load_or_build_caches_failure(tmp_path, monkeypatch):
    """抽不到也要落盘，否则每次 rerun 都会重读一遍 PDF。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "source.pdf").write_bytes(b"%PDF-fake")
    calls = []
    monkeypatch.setattr(financials, "extract", lambda data, **kw: calls.append(1) or None)
    assert financials.load_or_build("x") is None
    assert financials.load_or_build("x") is None
    assert len(calls) == 1
    assert financials.load_or_build("missing") is None
