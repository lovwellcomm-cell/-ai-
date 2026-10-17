"""深度度量的测试。

每条检查都配三种用例，缺一不可：

  · **正例** —— 该抓的能不能抓住
  · **注入反例** —— 把错误塞进去，检查必须响
  · **误报护栏** —— 从 30 份真实回答里抄来的原句，检查**不许响**

第三种最重要。这个项目已经栽过两次在"检查器自己不响或者乱响"上：
`eval_fidelity` 的三个 bug、`eval_depth` 用 `未找到|未披露` 误判弃答。
而这次写 `eval_rubric` 的过程中，误报护栏又当场抓出四个我自己的错
（负号、方向词、标记窗口过宽、无括号统计口径）。所以每个护栏都注明它是哪来的。

下面出现的真实回答片段，都出自 `eval_out/` 里 2026-10-16 那 5 组存档。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eval_rubric import (  # noqa: E402
    causal_sentences, citations, caliber, computed_rates, inferential_causation,
    quote_check, run, unit_conversions, unsourced_items,
)
from tools.rubric_questions import anchors  # noqa: E402

# 一份仿真的片段：数字取自真实年报（精达股份 2025）
CHUNK = {
    "text": "营业收入 24,673,670,269.16 22,322,579,930.98\n"
            "营业成本 22,489,950,220.41 20,302,000,000.00\n"
            "归属于母公司股东的净利润 599,143,119.39 561,708,879.75\n"
            "经营活动产生的现金流量净额 2,475,082,817.33 -589,153,155.66\n"
            "净利润 622,525,281.91 576,965,894.85\n"
            "加工制造业毛利率 5.83 减少0.13个百分点\n"
            "销售商品、提供劳务收到的现金 26,275,778,045.45 22,739,727,998.27",
    "section": "第八节财务报告", "subsection": "合并利润表", "page_start": 78, "page_end": 78,
}
HITS = [CHUNK]


# ---------------------------------------------------------------- 1 准确性

def test_out_of_range_citation_is_reported():
    """核心能力：引用了不存在的来源片段。`cited_numbers` 会静默丢掉它，
    整条链路里不留痕迹 —— 这条检查专门把它捞出来。"""
    r = citations("营业收入 1.00 元[1][9]。", 8)
    assert r["越界数"] == 1 and r["越界标记"] == ["[9]"]
    assert citations("营业收入 1.00 元[1][2]。", 8)["越界数"] == 0


def test_year_in_brackets_is_not_a_fabricated_source():
    """误报护栏：`[2025]` 会被同一个正则匹配上，而 2025 > 8。
    眼下 30 份回答里没出现，但别的提示词里模型就给年份加方括号。"""
    assert citations("报告期[2025]营业收入 1.00 元。", 8)["越界数"] == 0


def test_markdown_link_and_list_marker_are_not_citations():
    """误报护栏：`[1](url)` 是链接；行首的 `[1] ` 是列表标记。"""
    assert citations("见[1](https://example.com)附录。", 8)["引用编号"] == []
    assert citations("- 收入说明\n[1] 注：口径为合并。", 8)["引用编号"] == []


def test_quote_must_be_verbatim_but_only_attributive_ones():
    """只查「声称是原文」的引文。

    误报护栏全部是真的：模型引自己的结论、引用户的问话、引提示词自己的词，
    这些都不是在声称原文。全量查引号实测 31% 误报。
    """
    assert quote_check('说明为“营业收入”的数据[1]。', HITS)["非原文数"] == 0
    assert quote_check('说明为“营收大幅增长”。', HITS)["非原文数"] == 1
    # 以下都不该被检查
    for guard in ('趋势是“持续增长、增速放缓”。', '“那去年呢”指代不明确。',
                  '请按“本期值、上期值、变化额、变化率”对应。'):
        assert quote_check(guard, HITS)["转述引文数"] == 0, guard


def test_quote_normalises_whitespace_and_slashes():
    """误报护栏：片段里是 `减少0.13个百分点`，回答引成 `“减少 0.13 个百分点”`；
    以及 `“营业总收入/营业收入”` 是拿两个行标签拼的，要拆开逐段核对。"""
    hits = [{"text": "加工制造业毛利率 5.83 减少0.13个百分点\n"
                     "营业总收入 24,673,670,269.16\n营业收入 24,673,670,269.16"}]
    assert quote_check('列示“减少 0.13 个百分点”[1]。', hits)["非原文数"] == 0
    assert quote_check('列示“营业总收入/营业收入”[1]。', hits)["非原文数"] == 0


# ---------------------------------------------------------------- 3 逻辑链

def test_unit_conversion_consistency():
    assert unit_conversions("营业收入 246.74亿元（24,673,670,269.16元）。")["换算不一致"] == []
    bad = unit_conversions("营业收入 246.74亿元（24,673,670,269.16元）。")["换算一致"]
    assert bad == 1
    assert unit_conversions("营业收入 300.00亿元（24,673,670,269.16元）。")["换算不一致"] != []


def test_negative_sign_is_propagated():
    """误报护栏（实测 19/207 全折在这里）：负号不传播的话，
    下面这条会被判成不一致 —— 而 `-5.89亿元` 正是「跨表」那道题最核心的数字。"""
    r = unit_conversions("2024年经营活动现金流量净额为-5.89亿元（-589,153,155.66元）[2]。")
    assert r["换算不一致"] == [] and r["换算一致"] == 1


def test_sign_error_without_direction_word_is_still_caught():
    """上一条的代价：允许绝对值比对必须有个前提，否则真抄错符号会被放过。"""
    r = unit_conversions("经营活动现金流量净额为-5.89亿元（589,153,155.66元）。")
    assert len(r["换算不一致"]) == 1


def test_magnitude_written_with_a_direction_word_matches_the_signed_value():
    """误报护栏（真实原句）：外侧写的是差额的**大小**，括号里是**带符号**的值。"""
    r = unit_conversions("2024年经营现金流净额比净利润少11.66亿元（-1,166,119,050.51元，据[1]数字计算）[1]。")
    assert r["换算不一致"] == []
    # 「低」单字也要认 —— 只写「低于」覆盖不到这一句
    r2 = unit_conversions("2025年经营现金流比净利润高18.53亿元（1,852,557,535.42元）；"
                          "2024年则低11.66亿元（-1,166,119,050.51元，据片段数字计算）。")
    assert r2["换算不一致"] == [], r2["换算不一致"]


def test_paren_may_hold_a_note_and_units_may_differ():
    r = unit_conversions("差额18.53亿元（1,852,557,535.42元，据[1][2][7]数字计算）。")
    assert r["换算不一致"] == [] and r["换算一致"] == 1
    assert unit_conversions("收入783.06万元（7,830,636.65元）。")["换算不一致"] == []
    assert unit_conversions("**246.74亿元**（24,673,670,269.16元）")["换算不一致"] == []


def test_raw_value_and_converted_value_may_come_in_either_order():
    """误报护栏（真实原句，2026-10-16 23:54 那轮新语料才出现）：
    两种顺序都会写，四舍五入可能落在任意一边。容差只按外侧算的话，
    第二种会变成 ±0.005 **元**，等于要求分毫不差。"""
    assert unit_conversions("主营业务收入为238.83亿元（23,882,535,292.17元）[1]。")["换算不一致"] == []
    r = unit_conversions("主营业务收入为23,882,535,292.17元（238.83亿元）[1]。")
    assert r["换算不一致"] == [] and r["换算一致"] == 1


def test_missing_or_note_only_paren_is_informational_not_a_failure():
    """实测 39 处合法的「无括号」（结论行、推导值、列举），
    以及只有说明的括号 —— 这些是人读的清单，不是失败计数。"""
    r = unit_conversions("较2024年增加23.51亿元，增长10.53%。另增加约21.92亿元（据片段数字计算）。")
    assert r["换算不一致"] == [] and r["换算一致"] == 0
    assert r["仅说明括号"] == ["21.92亿元"]


def test_bare_yuan_values_are_not_counted_as_missing_parens():
    """口径收紧：只统计「换算后的单位」。否则每一个原始数值都会落进「无括号」，
    197 条噪声把真正要看的那几条淹掉。"""
    r = unit_conversions("营业收入24,673,670,269.16元，净利润599,143,119.39元。")
    assert r["无括号"] == []


def test_computed_rate_is_verified_against_the_answers_own_numbers():
    r = computed_rates("2025年为246.74亿元（24,673,670,269.16元），2024年为223.23亿元"
                       "（22,322,579,930.98元），增长10.53%（据片段数字计算）[4]。")
    assert r["自算率检查数"] == 1 and r["自算不一致数"] == 0


def test_the_real_arithmetic_error_is_caught():
    """**这条是真事故**：followup/跨表 写「增幅约 15.56%」，
    而它自己引用的 3,536,050,047.18 / 22,739,727,998.27 = 15.5501%，
    baseline 对同一个量算的是 15.55%。差 0.01 个百分点。"""
    answer = ("2025年销售商品收到的现金为262.76亿元（26,275,778,045.45元），"
              "较2024年227.40亿元（22,739,727,998.27元）增加35.36亿元"
              "（3,536,050,047.18元，据[4]数字计算），增幅约15.56%（据片段数字计算）[4]。")
    r = computed_rates(answer)
    assert r["自算不一致数"] == 1 and "15.56" in r["自算不一致"][0]["率"], r


def test_decline_rate_with_negative_operand():
    """误报护栏（真实原句）：`下降234.21%` 是对的 ——
    (−589,153,155.66 − 438,962,688.40) / 438,962,688.40 = −234.214%。
    `extract_numbers` 的正则不含前导负号，用它做池子会反解出 +34.21% 而误判。"""
    answer = ("2024年为-5.89亿元（-589,153,155.66元）[1]；2023年为4.39亿元（438,962,688.40元）[1]。"
              "2024年比2023年减少10.28亿元，下降234.21%（据片段数字计算）[1]。")
    assert computed_rates(answer)["自算不一致数"] == 0


def test_percentage_point_derivation_is_not_a_ratio():
    """误报护栏（真实原句）：由百分点变化推水平值，不是比率运算，本检查不建模。"""
    answer = "加工制造业毛利率为5.83%，上年同期约为5.96%（据“减少 0.13 个百分点”计算）[1][2]。"
    assert computed_rates(answer)["自算率检查数"] == 0


def test_a_marker_belonging_to_another_number_does_not_leak():
    """误报护栏（真实原句）：那个「据…计算」标记属于前面的「30.64亿元」，
    而 520.11% 是照抄年报的（带 [7]）。早先用 26 字宽松窗口时，这里凭空造出 3 个误报。"""
    answer = ("2025年经营现金流净额同比增加30.64亿元（3,064,235,972.99元，据[1][2][7]数字计算），"
              "变动比例为520.11%[7]。")
    assert computed_rates(answer)["自算率检查数"] == 0


def test_inference_marker_also_triggers_the_rate_check():
    """回归：`_MARK_CORE` 原来只认「计算/给出」，而扩展分析的核心标记是
    「据片段数据推断」—— 整个被跳过，等于给新功能开了个后门。"""
    answer = "营业收入246.74亿元，上年223.23亿元，增长约10.53%（据片段数据推断）[1]。"
    assert computed_rates(answer)["自算率检查数"] == 1


def test_inferential_causation_tripwire():
    """拿「推断」标记给原因打掩护。

    这种句子**有引用也有数字**，过得了一般的证据检查，但它说的其实是一个
    片段里没有的因果 —— 只有这条能把它挑出来。"""
    assert inferential_causation(
        "毛利率下降（据片段数据推断），主要是铜价上涨导致[1]。")["推断式归因数"] == 1
    # 结构性推断（不是在说原因）不算
    assert inferential_causation(
        "据片段数据推断，成本增速高于收入0.16个百分点[1]。")["推断式归因数"] == 0


def test_quoted_rate_without_marker_is_not_checked():
    answer = "2025年经营活动产生的现金流量净额同比变动比例为520.11%[7]。"
    assert computed_rates(answer)["自算率检查数"] == 0


# ---------------------------------------------------------------- 4 证据 / 5 解释层次

def test_truly_unsupported_item_is_listed():
    r = unsourced_items("- 公司经营情况良好，前景广阔。\n- 营业收入为246.74亿元[1]。")
    assert r["无引用条目数"] == 1 and "前景广阔" in r["无引用条目"][0]


def test_structural_lines_are_not_unsupported_claims():
    """误报护栏（全部是真实原句）：结论行、结构标签、口径说明、反问用户的话，
    本来就不该带引用。不排除掉的话，215 个条目里被标出的**全部合法**。"""
    answer = ("一句话结论：近三年营业收入均增长。\n分点依据：\n1. 营业收入：逐年增长\n"
              "以上均为合并报表口径。\n以上为合并报表口径。\n请明确你问的是哪个指标，我可以继续对应。\n"
              "因此，片段可支持的毛利率变动解释是：成本增速略快。\n依据如下：\n")
    assert unsourced_items(answer)["无引用条目数"] == 0


def test_causal_sentence_without_evidence_is_listed():
    r = causal_sentences("毛利率下降主要因为铜价上涨。")
    assert r["无据归因数"] == 1
    assert causal_sentences("毛利率下降主要因为铜价上涨[2]。")["无据归因数"] == 0
    assert causal_sentences("毛利率下降主要因为铜价上涨，约0.13个百分点。")["无据归因数"] == 0


def test_saying_a_reason_was_not_disclosed_is_not_an_ungrounded_attribution():
    """误报护栏 —— 这一条极其重要，全是真实原句。

    实测 17 个被标出的因果句里 16 个是模型在**正确地报告「年报未披露原因」**。
    不过滤的话，这个检查专门惩罚诚实 —— 而这正是这个项目上次栽跟头的同一个形状
    （拿 `未找到|未披露` 匹配弃答，把「未作说明，但从片段可以看出…」判成了弃答）。
    """
    for guard in ("年报片段未对“毛利率变动原因”作出专门说明。",
                  "片段未进一步解释具体原因。",
                  "这解释了收入、成本变动，但没有单独解释毛利率变动原因。",
                  "因此，只能回答：毛利率变动原因未明确披露。"):
        assert causal_sentences(guard)["无据归因数"] == 0, guard


# ---------------------------------------------------------------- 2 完整性

def test_anchors_pass_on_a_real_answer():
    a = ("一句话结论：2025年营业收入为 **246.74亿元**（24,673,670,269.16元），为合并报表口径。")
    r = anchors("简单", a)
    assert r["锚点全中"] is True


def test_anchors_fail_on_a_wrong_value():
    assert anchors("简单", "2025年营业收入为100亿元。")["锚点全中"] is False


def test_followup2_anchor_only_checks_the_decidable_part():
    """追问-2 真正考的那件事（有没有偷偷替用户猜一个指代）机器判不了，
    所以只锚得住「提到了 2024 且给了数」。这个局限写在 rubric_questions.py 里。"""
    assert anchors("追问-2", "若“去年”指2024年度：营业收入223.23亿元，净利润5.62亿元，总资产126.27亿元。")["锚点全中"]
    assert not anchors("追问-2", "指2024年。")["锚点全中"]


def test_caliber_only_fires_when_only_parent_company_data_was_found():
    """条件触发。提示词第 3 条的要求是「**只找到**母公司口径时」才注明，
    所以不能写成"回答里有没有'母公司'" —— 而 `归属于母公司股东的净利润`
    是固定科目名，每篇回答都有，naive 匹配会被刷成永远通过。"""
    merged = [{"subsection": "合并利润表", "text": "x"}]
    parent = [{"subsection": "母公司利润表", "text": "x"}]
    assert caliber("营业收入 1 元[1]。", merged)["口径触发"] is False
    assert caliber("营业收入 1 元[1]。", merged + parent)["口径触发"] is False   # 合并也在，不触发

    r = caliber("营业收入 1 元[1]。", parent)
    assert r["口径触发"] and r["口径缺注"]
    # 这句里就有"母公司"三个字，但它不是口径说明 —— 必须仍然报缺注
    still = caliber("2025年归属于母公司股东的净利润为5.99亿元[1]。", parent)
    assert still["口径缺注"]
    assert caliber("以上均为母公司报表口径。营业收入 1 元[1]。", parent)["口径缺注"] is None


# ---------------------------------------------------------------- 组装

def test_run_is_safe_on_empty_input():
    r = run("", [], "")
    assert r["金额个数"] == 0 and r["越界数"] == 0 and r["无引用条目数"] == 0


def test_no_rate_is_emitted_without_its_denominator():
    """结构性防线：这个语料里已经有过一个漂亮的比率被当成质量信号读
    （`引用命中率` 对「覆盖」恒为 0.29，其实只是 `分点依据：` 被当成了一句没引用的话）。
    所以细则层只允许 `有据可依率` 这一个「率」，而它的分母 `金额个数` 永远在同一行。"""
    keys = set(run("营业收入 1 元[1]。", HITS, "简单"))
    assert {k for k in keys if k.endswith("率")} == {"有据可依率"}
    assert "金额个数" in keys
