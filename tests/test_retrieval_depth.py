"""检索深度相关的测试：来源多样性限流、报表年份表头传播。

这两项都不调用大模型，纯函数逻辑，所以用合成片段就能测准。
"""
import numpy as np
import pytest

from rag import config
from rag.generator import format_context
from rag.retriever import Retriever, extract_header, needs_header


def fake_embed(texts):
    return np.zeros((len(texts), 4), dtype="float32")


def make_chunks(specs):
    """specs: [(section, subsection, text), ...]，chunk_id 就是下标。"""
    return [
        {"chunk_id": i, "section": s, "subsection": sub, "text": t,
         "page_start": i + 1, "page_end": i + 1, "index_text": t}
        for i, (s, sub, t) in enumerate(specs)
    ]


def make_retriever(chunks):
    return Retriever(chunks, np.zeros((len(chunks), 4), dtype="float32"), embed=fake_embed)


# ---------------------------------------------------------------- 表头抽取

HEADER_TEXT = "合并利润表\n2025 年 1—12 月\n单位：元 币种：人民币\n项目\n附\n注\n2025 年度 2024 年度\n一、营业总收入 24,673,670,269.16 22,322,579,930.98\n"


def test_extract_header_stops_at_first_amount_line():
    header = extract_header(HEADER_TEXT)
    assert "合并利润表" in header
    assert "2025 年度 2024 年度" in header
    # 数据行不能被当成表头
    assert "营业总收入" not in header
    assert "24,673" not in header


def test_extract_header_keeps_year_line_below_the_eighth_row():
    """回归：合并资产负债表的年份列标在第 9 行。

    上限一旦设成 8，最关键的那一行会被截掉，传播出去的表头等于没用 —— 这是实际踩过的坑。
    """
    balance = ("合并财务报表\n合并资产负债表\n2025 年 12 月 31 日\n"
               "编制单位：某某股份有限公司\n单位：元 币种：人民币\n项目\n附\n注\n"
               "2025 年 12 月 31 日 2024 年 12 月 31 日\n"
               "货币资金 2,069,148,661.10 1,117,554,428.04\n")
    header = extract_header(balance)
    assert "2025 年 12 月 31 日 2024 年 12 月 31 日" in header
    assert "货币资金" not in header


def test_needs_header_detects_year_and_unit_markers():
    assert needs_header(HEADER_TEXT) is False
    assert needs_header("财务费用 123,711,547.40 146,963,561.21\n") is True


# ---------------------------------------------------------------- 表头传播

@pytest.fixture
def statement_chunks():
    return make_chunks([
        ("第八节财务报告", "合并利润表", HEADER_TEXT),                       # 0 自带表头
        ("第八节财务报告", "合并利润表", "财务费用 123,711,547.40 146,963,561.21\n"),   # 1
        ("第八节财务报告", "合并利润表", "资产处置收益 -3,980.13 -545,117.42\n"),        # 2
    ])


def test_header_propagates_backwards_within_the_same_subsection(statement_chunks):
    r = make_retriever(statement_chunks)
    hits = r._attach_headers([dict(statement_chunks[2])])
    assert "2025 年度 2024 年度" in hits[0]["context_header"]


def test_chunk_that_already_has_a_header_is_left_alone(statement_chunks):
    r = make_retriever(statement_chunks)
    hits = r._attach_headers([dict(statement_chunks[0])])
    assert "context_header" not in hits[0]


def test_header_does_not_leak_across_subsection_boundary():
    """跨出同一个子章节就必须放弃，否则会把别的报表的年份安到这上面来。"""
    chunks = make_chunks([
        ("第八节财务报告", "合并现金流量表", HEADER_TEXT),
        ("第八节财务报告", "合并利润表", "财务费用 123,711,547.40 146,963,561.21\n"),
    ])
    r = make_retriever(chunks)
    assert "context_header" not in r._attach_headers([dict(chunks[1])])[0]


def test_header_does_not_leak_across_section_boundary():
    chunks = make_chunks([
        ("第三节管理层讨论与分析", "主营业务分析", HEADER_TEXT),
        ("第八节财务报告", "主营业务分析", "财务费用 123,711,547.40 146,963,561.21\n"),
    ])
    r = make_retriever(chunks)
    assert "context_header" not in r._attach_headers([dict(chunks[1])])[0]


def test_first_chunk_without_any_preceding_header_gets_nothing():
    chunks = make_chunks([("第八节财务报告", "合并利润表", "财务费用 123,711,547.40 1,00.00\n")])
    r = make_retriever(chunks)
    assert "context_header" not in r._attach_headers([dict(chunks[0])])[0]


def test_attach_headers_can_be_switched_off(statement_chunks, monkeypatch):
    monkeypatch.setattr(config, "HEADER_PROPAGATION", False)
    r = make_retriever(statement_chunks)
    assert "context_header" not in r._attach_headers([dict(statement_chunks[2])])[0]


def test_attach_headers_does_not_mutate_the_original_hit(statement_chunks):
    r = make_retriever(statement_chunks)
    original = dict(statement_chunks[2])
    r._attach_headers([original])
    assert "context_header" not in original


def test_format_context_prepends_the_propagated_header(statement_chunks):
    r = make_retriever(statement_chunks)
    hit = r._attach_headers([dict(statement_chunks[2])])[0]
    text = format_context([hit])
    assert "表头" in text
    assert "2025 年度 2024 年度" in text
    assert text.index("2025 年度") < text.index("资产处置收益")


# ---------------------------------------------------------------- 多样性限流

@pytest.fixture
def skewed_chunks():
    """模拟真实语料：一个章节压倒性地多。"""
    return make_chunks(
        [("第八节财务报告", f"附注{i}", f"片段 {i}\n") for i in range(20)]
        + [("第三节管理层讨论与分析", f"分析{i}", f"分析 {i}\n") for i in range(5)]
    )


def fused_from(chunks, order):
    """按给定顺序造一个分数递减的 RRF 结果。"""
    return {i: 1.0 / (60 + r + 1) for r, i in enumerate(order)}


def test_diversity_gives_minority_sections_a_share(skewed_chunks, monkeypatch):
    """限流真正要保证的事：冷门章节能拿到位置。

    配额只约束主循环，回填会超出配额，所以这里断言的是「冷门章节至少拿到
    min(配额, 可用数) 个位置」，而不是精确的分布。
    """
    order = [c["chunk_id"] for c in skewed_chunks[:15]] + \
            [c["chunk_id"] for c in skewed_chunks[20:]]
    fused = fused_from(skewed_chunks, order)

    monkeypatch.setattr(config, "MAX_PER_SECTION", 999)          # 等于关闭限流
    without = make_retriever(skewed_chunks)._select_diverse(fused, 8)

    monkeypatch.setattr(config, "MAX_PER_SECTION", 3)
    with_cap = make_retriever(skewed_chunks)._select_diverse(fused, 8)

    minority = "第三节管理层讨论与分析"
    assert sum(skewed_chunks[i]["section"] == minority for i in without) == 0
    assert sum(skewed_chunks[i]["section"] == minority for i in with_cap) == 3
    assert len(with_cap) == 8


def test_diversity_backfills_when_candidates_run_out(skewed_chunks, monkeypatch):
    """候选不足时必须回填，不能因为限流就少给片段。"""
    monkeypatch.setattr(config, "MAX_PER_SECTION", 1)
    r = make_retriever(skewed_chunks)
    order = [c["chunk_id"] for c in skewed_chunks[:4]]      # 全来自同一节，只有 4 个候选
    picked = r._select_diverse(fused_from(skewed_chunks, order), 8)
    assert len(picked) == 4


def test_diversity_keeps_score_order_among_picked(skewed_chunks, monkeypatch):
    monkeypatch.setattr(config, "MAX_PER_SECTION", 2)
    r = make_retriever(skewed_chunks)
    order = [c["chunk_id"] for c in skewed_chunks]
    fused = fused_from(skewed_chunks, order)
    picked = r._select_diverse(fused, 6)
    scores = [fused[i] for i in picked]
    assert scores == sorted(scores, reverse=True)


def test_diversity_is_a_pure_function_of_its_inputs(skewed_chunks, monkeypatch):
    """retriever 声明过「不修改任何共享状态，多个会话可同时调用」，别破坏它。"""
    monkeypatch.setattr(config, "MAX_PER_SECTION", 3)
    r = make_retriever(skewed_chunks)
    fused = fused_from(skewed_chunks, [c["chunk_id"] for c in skewed_chunks])
    before = dict(fused)
    r._select_diverse(fused, 8)
    assert fused == before


# ---------------------------------------------------------------- 追问：指代消解

import json                       # noqa: E402
from types import SimpleNamespace  # noqa: E402

from rag.retriever import llm_rewrite  # noqa: E402


class FakeLLM:
    """只实现 chat.completions.create，够 llm_rewrite 用。"""

    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        content = json.dumps(self.payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_rewrite_without_history_keeps_the_old_prompt_and_cap():
    """首轮行为必须一字不变 —— 有既有测试钉着「第一条检索词」，别改坏。"""
    llm = FakeLLM({"queries": ["2025年 营业利润"]})
    out = llm_rewrite("去年营业利润多少", year="2025", client=llm)
    assert out == ["2025年 营业利润"]
    system = llm.calls[0]["messages"][0]["content"]
    assert "之前的问题" not in system
    assert llm.calls[0]["messages"][1]["content"] == "去年营业利润多少"


def test_rewrite_with_history_asks_for_pronoun_resolution():
    llm = FakeLLM({"queries": ["2024年 营业收入"]})
    llm_rewrite("那去年呢", year="2025", client=llm, history=["营业收入和归母净利润分别是多少？"])
    system = llm.calls[0]["messages"][0]["content"]
    user = llm.calls[0]["messages"][1]["content"]
    assert "之前的问题" in system and "追问" in system
    assert "营业收入和归母净利润" in user and "那去年呢" in user


def test_rewrite_history_is_capped(monkeypatch):
    monkeypatch.setattr(config, "FOLLOWUP_HISTORY_TURNS", 2)
    llm = FakeLLM({"queries": ["x"]})
    llm_rewrite("那去年呢", year="2025", client=llm, history=["问题一", "问题二", "问题三"])
    user = llm.calls[0]["messages"][1]["content"]
    assert "问题二" in user and "问题三" in user
    assert "问题一" not in user          # 只带最近两轮


def test_rewrite_failure_is_reported_through_diag():
    """这个函数吞掉所有异常 —— 没有 diag 就完全看不出追问已经失效了。"""
    diag = {}
    out = llm_rewrite("那去年呢", year="2025", client=FakeLLM(error=TimeoutError("超时")),
                      history=["营业收入是多少？"], diag=diag)
    assert out == []
    assert diag["rewrite"] == "fallback"
    assert "TimeoutError" in diag["rewrite_error"]


def test_rewrite_success_is_reported_through_diag():
    diag = {}
    llm_rewrite("那去年呢", year="2025", client=FakeLLM({"queries": ["2024年 营业收入"]}), diag=diag)
    assert diag["rewrite"] == "ok"


@pytest.fixture
def followup_retriever():
    return make_retriever(make_chunks([("第八节财务报告", "合并利润表", "营业收入 1.00 2.00\n")]))


def test_followup_drops_the_raw_rule_rewrite(followup_retriever):
    """「那2024年呢」的词元几乎命中所有片段，留着会把 RRF 冲淡。"""
    llm = FakeLLM({"queries": ["2024年 营业收入"]})
    queries = followup_retriever._expand("那去年呢", llm, True, history=["营业收入是多少？"])
    assert queries == ["2024年 营业收入"]
    assert not any("那" in q for q in queries)


def test_followup_falls_back_to_rule_rewrite_when_the_model_fails(followup_retriever):
    """退化路径必须仍然给得出检索词，不能返回空。"""
    queries = followup_retriever._expand("去年营业利润多少", FakeLLM(error=RuntimeError("boom")), True,
                                         history=["营业收入是多少？"])
    assert queries, "改写失败时不能没有检索词"
    assert "2024" in queries[0] or "营业利润" in queries[0]


def test_first_turn_keeps_rule_rewrite_first(followup_retriever):
    """没有历史时维持原行为：规则改写排在最前。"""
    llm = FakeLLM({"queries": ["2025年 合并利润表 营业利润"]})
    queries = followup_retriever._expand("去年的营业利润", llm, True)
    assert "营业利润" in queries[0]
    assert len(queries) == 2
