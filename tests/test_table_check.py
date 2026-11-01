"""回答里 Markdown 表格的逐格核对（rag/fidelity.py:unverified_in_tables）。

表格让数字更好读，也让编造的数字更像事实，所以每个单元格都要过一遍。
误报和漏报都有代价：误报会让投资者不信任正确的数字，漏报会放过编造。
"""
from rag.fidelity import table_cells, unverified_in_tables

# 片段：精达股份 2025 主要会计数据（元）+ 一行带百分比的毛利率
HITS = [{"text": "单位：元 币种：人民币\n"
                 "营业收入 24,673,670,269.16 22,322,579,930.98 10.53 17,905,865,235.94\n"
                 "归属于上市公司股东的净利润 599,143,119.39 561,708,879.75 6.66 426,452,861.87\n"
                 "综合毛利率 11.70% 11.10%"}]

GOOD = """营收与利润双增，净利率小幅下滑。

| 项目 | 2025年 | 2024年 | 变化 | 来源 |
|---|---|---|---|---|
| 营业收入 | 246.74 亿元 | 223.23 亿元 | +10.53% | [1] |
| 归母净利润 | 5.99 亿元 | 5.62 亿元 | +6.66% | [1] |
| 归母净利率 | 2.43% | 2.52% | -0.09 个百分点 | [1] |
| 综合毛利率 | 11.70% | 11.10% | +0.60pct | [1] |

- 净利率下滑主要因费用增长快于收入[1]。
"""


def test_correct_table_has_nothing_flagged():
    """直接引用的金额、片段里的百分比、由金额算出的净利率与变化率、由百分比算出的百分点 —— 全部通过。"""
    check = unverified_in_tables(GOOD, HITS)
    assert check["unverified"] == []
    assert check["numbers"] == 12          # 4 行 × 3 个数值单元格


def test_fabricated_numbers_are_flagged():
    bad = GOOD.replace("| 归母净利润 | 5.99 亿元", "| 归母净利润 | 8.88 亿元") \
              .replace("+10.53%", "+17.77%") \
              .replace("+0.60pct", "+2.60pct")
    assert set(unverified_in_tables(bad, HITS)["unverified"]) == {"8.88 亿元", "17.77%", "2.60pct"}


def test_citation_numbers_and_row_names_are_not_data():
    """「[1]」是引用编号，「前5名客户」里的 5 是名字 —— 都不该被当成财务数字去核对。"""
    ans = "| 项目 | 金额 | 来源 |\n|---|---|---|\n| 前5名客户 | 246.74 亿元 | [1] |\n"
    check = unverified_in_tables(ans, HITS)
    assert check == {"numbers": 1, "unverified": []}


def test_header_row_is_not_data():
    """表头（「2025年」「2024年」）只是列名，不参与核对。"""
    assert table_cells("| 项目 | 2025年 | 2024年 |\n|---|---|---|\n| 营业收入 | 1 | 2 |") == \
        [("1", "1"), ("2", "2")]
    check = unverified_in_tables("| 项目 | 2025年 |\n|---|---|\n| 营业收入 | 246.74 亿元 |", HITS)
    assert check == {"numbers": 1, "unverified": []}


def test_unit_in_header_applies_to_bare_numbers():
    """「金额（亿元）」列下只写 23.88：要按 23.88 亿元去找出处，而不是 23.88 元。

    实测踩到的：模型把单位写进表头、单元格只留数字。不补单位的话只能靠推导规则碰运气。
    """
    ans = ("| 项目 | 金额（亿元） | 毛利率变化（百分点） | 来源 |\n|---|---|---|---|\n"
           "| 营业收入 | 246.74 | 减少0.60 | [1] |\n"
           "| 编造 | 9.99 | 增加7.00 | [1] |\n")
    check = unverified_in_tables(ans, HITS)
    assert check["numbers"] == 4
    # 报告原样的单元格写法，界面才能在单元格里找到它
    assert check["unverified"] == ["9.99", "增加7.00"]


def test_header_unit_is_not_forced_onto_mixed_cells():
    """单元格已经自带单位或混着别的文字时，不套表头单位。"""
    cells = table_cells("| 项目 | 金额（亿元） |\n|---|---|\n| 营业收入 | 约 246.74 亿元 |\n| 年份 | 2025年 |")
    assert cells == [("约 246.74 亿元", "约 246.74 亿元"), ("2025年", "2025年")]


def test_ten_thousand_yuan_table_in_snippet():
    """片段以万元为单位（时代新材）：答案写 214.71 亿元是对的，不能误报。"""
    hits = [{"text": "单位：万元 币种：人民币\n营业收入 2,147,054.45 2,005,500.74 7.06 1,753,786.71"}]
    ans = "| 项目 | 2025年 | 2024年 |\n|---|---|---|\n| 营业收入 | 214.71 亿元 | 200.55 亿元 |\n"
    assert unverified_in_tables(ans, hits)["unverified"] == []


def test_no_table_returns_none():
    assert unverified_in_tables("营业收入 246.74 亿元[1]。", HITS) is None
    assert unverified_in_tables("", HITS) is None


def test_table_without_separator_row_still_counts():
    ans = "| 营业收入 | 246.74 亿元 | [1] |\n| 编造 | 9.99 亿元 | [1] |"
    assert unverified_in_tables(ans, HITS)["unverified"] == ["9.99 亿元"]
