"""扩展分析（第二次模型调用）的测试。

这个功能最危险的失败形态是**把推断写成事实**：让模型解释"为什么"，它就会用行业常识
补一个听起来合理、但年报里没有的原因。所以提示词里的约束和运行时的兜底都要钉住。

三类用例：
  · 提示词必须含那几条硬约束（和 ANSWER_BUDGET_RULE 的测试同理，防止有人把它改软）
  · `clean_extension` 的运行时兜底（没有出处就不算解释）
  · `analyze` 的降级路径（截断不能当成"无补充"）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import config, generator  # noqa: E402
from rag.generator import (  # noqa: E402
    ABSTAIN_MARK, ANALYZE_SYSTEM_PROMPT, EXTENSION_HEADING, analyze, build_analyze_messages,
    build_messages, clean_extension, join_answer, needs_explanation, should_analyze,
)

CHUNKS = [
    {"text": "营业收入 24,673,670,269.16 22,322,579,930.98\n营业成本 22,489,950,220.41",
     "section": "第八节财务报告", "subsection": "合并利润表", "page_start": 78, "page_end": 78},
    {"text": "营业收入变动原因说明为“主要是原材料价格上涨及销量增加所致”",
     "section": "第三节管理层讨论与分析", "subsection": "", "page_start": 12, "page_end": 12},
]
FIRST = "结论：2025年营业收入 246.74 亿元（24,673,670,269.16元）[1]，同比增长 10.53%[1]。"


def stream_client(parts, finish="stop", error=None):
    """仿流式响应。形状与 rag/generator.generate 消费的一致。"""
    class C:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            if error:
                raise error
            events = [
                SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=p), finish_reason=None)])
                for p in parts
            ]
            events.append(SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None), finish_reason=finish)]))
            return iter(events)

    return C()


# ---------------------------------------------------------------- 提示词

def test_prompt_carries_the_grounding_constraints_verbatim():
    """这几条是用户定的边界，不许被改软成"尽量"「一般为」。"""
    p = ANALYZE_SYSTEM_PROMPT
    assert "不得引入片段之外" in p          # 没有外部知识
    assert "年报未披露具体原因" in p        # 没写原因就明说
    assert "据片段数据推断" in p            # 结构性推断必须标注
    assert "不能用来给原因" in p            # 标注不能拿来给因果打掩护
    assert "无补充" in p                    # 防止凑字数的逃生口
    assert "{budget}" in p                  # 字数上限必须带数字


def test_analyze_messages_reuse_the_same_chunk_numbering():
    """[n] 必须和首轮回答指向同一批片段 —— 重新编号会让引用全错位。"""
    msgs = build_analyze_messages("营收为什么涨", CHUNKS, FIRST)
    user = msgs[1]["content"]
    assert "[1]（第八节财务报告 · 合并利润表，第78页）" in user
    assert "[2]（第三节管理层讨论与分析，第12页）" in user
    assert FIRST in user and "营收为什么涨" in user
    # 片段块与首轮生成完全一致
    assert generator.format_context(CHUNKS) in user
    assert str(config.ANALYZE_CHAR_BUDGET) in msgs[0]["content"]


def test_analyze_messages_carry_the_follow_up_history():
    """扩展段也要知道这一轮是不是追问 —— 否则它会解释错对象。"""
    user = build_analyze_messages("那去年呢", CHUNKS, FIRST,
                                  history=["营业收入和归母净利润分别是多少？"])[1]["content"]
    assert "【对话历史】" in user and "那去年呢" in user


def test_first_pass_prompt_is_untouched():
    """第二次调用不许改变首轮的行为 —— 这是回归测试，不是新需求。"""
    msgs = build_messages("营收多少", CHUNKS)
    assert len(msgs) == 2 and "年报未披露具体原因" not in msgs[0]["content"]


def test_should_analyze_skips_abstention():
    assert should_analyze(FIRST) is True
    assert should_analyze("年报中未找到相关信息。片段里只有营业收入。") is False


def test_should_analyze_skips_empty_answer():
    assert should_analyze("   ") is False


def test_lookup_questions_skip_the_second_call():
    """实测 18 题（两份年报 × 三道）：题面带「是多少 / 分别是多少」的 6 题**全部**
    返回「无补充」，一次例外都没有；带解释性措辞的 9 题出了 8 次扩展。

    所以这一类不值得再调一次模型 —— 省下的是真实的调用，不是延迟。
    """
    for q in ("2025年营业收入是多少？", "营业收入和归母净利润分别是多少？"):
        assert needs_explanation(q) is False
        assert should_analyze(FIRST, q) is False
    for q in ("近三年营业收入和归母净利润的变化趋势如何？",
              "毛利率变动的原因是什么？",
              "经营活动产生的现金流量净额与净利润的差异说明了什么？"):
        assert needs_explanation(q) is True
        assert should_analyze(FIRST, q) is True


def test_unclear_questions_default_to_running():
    """判不准就跑 —— 宁可多调一次，也不要把本来能解释的题漏掉。

    「那去年呢？」没有解释性措辞也没有查数措辞，实测 1/3 会产出扩展，所以要跑。"""
    for q in ("那去年呢？", "毛利率怎么样？", ""):
        assert needs_explanation(q) is True


def test_abstention_wins_over_the_question_shape():
    """哪怕题目明显要解释，弃答时也不做 —— 没有材料可解释。"""
    assert should_analyze(f"{ABSTAIN_MARK}。", "毛利率变动的原因是什么？") is False


def test_should_analyze_respects_the_switch(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_ENABLED", False)
    assert should_analyze(FIRST) is False


# ---------------------------------------------------------------- 运行时兜底

def test_no_addition_means_no_extension():
    assert clean_extension(FIRST, "无补充", 2) == ("", "no_add")


def test_model_written_heading_is_stripped():
    """模型有时会自己写一遍标题，不去掉就会出现两层标题。"""
    text, status = clean_extension(FIRST, "**扩展分析**\n营业收入增长主要来自主营业务[1]。", 2)
    assert status == "ok" and text.startswith("营业收入增长") and "**" not in text


def test_sentence_repeating_the_first_answer_is_dropped():
    """最可能的失败形态：把首轮的话换个位置再说一遍。"""
    assert clean_extension(FIRST, FIRST, 2) == ("", "duplicate")
    text, status = clean_extension(
        FIRST, FIRST + "成本增速慢于收入，毛利率因此抬升[1]。", 2)
    assert status == "ok" and "成本增速" in text and "246.74" not in text


def test_extension_without_any_source_is_dropped():
    """**最关键的一条。** 没有出处就不算解释：
    一句话说"主要是因为铜价上涨"，既没有 [n] 也没说未披露 —— 那就是编的。"""
    assert clean_extension(FIRST, "营业收入增长主要是因为铜价上涨。", 2) == ("", "ungrounded")


def test_unsourced_but_honest_no_disclosure_is_kept():
    """明说「未披露」的句子本来就不需要有出处 —— 这正是在遵守规则。"""
    text, status = clean_extension(FIRST, "年报未披露具体原因。", 2)
    assert status == "ok" and "未披露" in text


def test_over_budget_extension_is_cut_at_a_sentence_boundary(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_CHAR_BUDGET", 10)     # 上限 15 字
    text, status = clean_extension(
        FIRST, "第一句有出处[1]。第二句也有出处[1]而且很长很长很长很长。", 2)
    assert status == "trimmed" and text.endswith("。") and "第二句" not in text


def test_empty_extension_is_empty():
    assert clean_extension(FIRST, "", 2) == ("", "empty")


# ---------------------------------------------------------------- 调用与降级

def test_analyze_returns_cleaned_text():
    diag = {}
    client = stream_client(["成本增速慢于收入，", "毛利率因此抬升[1]。"])
    out = analyze("营收为什么涨", CHUNKS, FIRST, client=client, diag=diag)
    assert out == "成本增速慢于收入，毛利率因此抬升[1]。" and diag["analyze"] == "ok"
    # 用的必须是流式：非流式下 timeout 是总时长，会把推理很久的合法调用误杀
    assert client.calls[0]["stream"] is True
    assert client.calls[0]["max_tokens"] == config.ANALYZE_MAX_TOKENS


def test_truncated_call_is_not_mistaken_for_no_addition():
    """`max_tokens` 把推理和正文一起算，截断会表现成「正文为空」——
    正是当年查询改写静默失效的形态，必须响亮地失败，不能当成「无补充」。"""
    diag = {}
    client = stream_client([], finish="length")
    with pytest.raises(RuntimeError, match="截断"):
        analyze("营收为什么涨", CHUNKS, FIRST, client=client, diag=diag)
    assert diag["analyze"] == "truncated"


def test_api_error_propagates_for_the_caller_to_degrade():
    """调用方（app / eval）负责接住异常并保留首轮回答。"""
    with pytest.raises(TimeoutError):
        analyze("营收为什么涨", CHUNKS, FIRST,
                client=stream_client([], error=TimeoutError("timeout")))


# ---------------------------------------------------------------- 拼接

def test_join_answer_puts_the_extension_in_its_own_paragraph():
    out = join_answer(FIRST, "成本增速慢于收入[1]。")
    assert out == f"{FIRST}\n\n{EXTENSION_HEADING}\n成本增速慢于收入[1]。"


def test_no_extension_means_no_dangling_heading():
    assert join_answer(FIRST, "") == FIRST
