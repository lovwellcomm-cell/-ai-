"""agent1 规划器：模型输出的校验、规则兜底、按计划选报告（不调模型）。

模型那一半的效果用 tools/eval_intent.py 拿真实模型测；这里测的是「模型说错了也不会被带偏」。
"""
from datetime import date

import pytest

from rag import planner
from ui import memory
from ui.turn import plan_for, quick_entry

TODAY = date(2026, 11, 2)
DOCS = [
    {"doc_id": "jd25", "company": "精达股份", "year": "2025", "demo": True},
    {"doc_id": "jd26q3", "company": "精达股份", "year": "2026", "period": "Q3"},
    {"doc_id": "mt24", "company": "贵州茅台", "year": "2024"},
]


def _data(**kw):
    base = {"restated": "", "intent": "report", "company": "", "multiple": False, "year": None,
            "period": None, "card": False, "subquestions": [], "queries": [],
            "clarify": {"question": "", "options": []}, "reply": ""}
    base.update(kw)
    return base


def norm(q, history=(), **kw):
    return planner.normalize(_data(**kw), q, list(history), TODAY)


def test_bad_output_returns_none_for_fallback():
    assert planner.normalize({"intent": "瞎写"}, "营收", [], TODAY) is None
    assert planner.normalize("not a dict", "营收", [], TODAY) is None


def test_invented_company_and_year_are_dropped():
    u = norm("营收多少", company="宁德时代", year=2023)
    assert u["target"]["company"] == "" and u["target"]["year"] is None


def test_company_from_history_is_kept():
    u = norm("它的现金流呢", history=["七匹狼利润率变化如何"], company="七匹狼")
    assert u["target"]["company"] == "七匹狼"


def test_rules_win_on_period_and_latest():
    u = norm("最新财报如何", period="FY")
    assert u["target"]["latest"] == "any" and u["target"]["period"] == "Q3"
    u = norm("今年前三季度营收", year=2025)          # 模型把「今年」换算错了：原文没有 2025 → 丢掉，用规则换算
    assert (u["target"]["year"], u["target"]["period"]) == (2026, "Q3")


def test_model_period_used_when_rules_silent():
    assert norm("中期业绩", period="H1")["target"]["period"] == "H1"


def test_other_company_is_never_out_of_scope():
    """真实踩到的：模型把「茅台2024年营收多少」判成超范围。"""
    u = norm("茅台2024年营收多少", intent="out_of_scope", company="茅台", year=2024)
    assert u["intent"] == "report"


def test_rule_industry_and_trend():
    assert norm("行业最新动态")["intent"] == "industry"
    assert norm("近几年营收趋势")["intent"] == "trend"


def test_clarify_needs_options():
    assert norm("对比一下", intent="clarify", clarify={"question": "比什么？", "options": ["只有一个"]})["intent"] == "report"
    u = norm("对比一下", intent="clarify",
             clarify={"question": "比什么？", "options": ["2025 与 2024 年营收对比？", "毛利率对比？"]})
    assert u["intent"] == "clarify" and len(u["clarify"]["options"]) == 2 and not u["card"]


def test_card_rules():
    assert norm("毛利率多少", card=False)["card"] is True        # 模型漏判，科目兜底
    assert norm("今天天气怎么样", intent="out_of_scope", card=True)["card"] is False
    assert norm("前五大客户的收入占比", card=False)["card"] is False   # 客户话题不出卡
    assert norm("近几年发展如何", intent="trend")["card"] is True


def test_focus_block_and_describe():
    u = norm("那2024年呢", history=["2025年营业收入"], restated="精达股份2024年营业收入是多少",
             subquestions=["2024年营业收入"], year=2024)
    block = planner.focus_block(u)
    assert "理解为：精达股份2024年营业收入是多少" in block and "以【问题】为准" in block
    assert planner.focus_block(dict(u, source="fallback")) == ""
    assert planner.describe(u).startswith("答报告 · 2024")


# ---------------------------------------------------------------- 按计划选报告

def _plan(u, cur_year="2025", cur_period="FY"):
    return plan_for(u, current_company="精达股份", current_year=cur_year,
                    current_period=cur_period, docs=DOCS, today=TODAY)


def test_trend_on_quarterly_switches_to_annual():
    """用户实际遇到的：停在三季报上问「近几年发展如何」→ 换到最新年报。"""
    u = norm("近几年发展如何", intent="trend")
    assert _plan(u, "2026", "Q3") == {"action": "switch", "doc": DOCS[0]}
    assert _plan(u) == {"action": "current"}


def test_health_on_quarterly_stays_for_merged_check():
    """财报体检（用户 2026-11-03 选的做法）：季报上体检不换报告，app.py 以年报 9 项为底合并。
    「年报体检」没写年份也一样；写了哪一期（「三季报体检」「2025 年报体检」）照他说的。"""
    on_q3 = {"intent": "health", "target": {"company": "", "year": None, "period": None}}
    assert _plan(on_q3, "2026", "Q3") == {"action": "current"}
    annual_word = {"intent": "health", "target": {"company": "", "year": None, "period": "FY"}}
    assert _plan(annual_word, "2026", "Q3") == {"action": "current"}
    named = {"intent": "health", "target": {"company": "", "year": None, "period": "Q3"}}
    assert _plan(named, "2026", "Q3") == {"action": "current"}
    year_named = {"intent": "health", "target": {"company": "", "year": 2025, "period": "FY"}}
    assert _plan(year_named, "2026", "Q3") == {"action": "switch", "doc": DOCS[0]}
    assert planner.describe(dict(on_q3, intent="health")).startswith("财报体检")


def test_annual_without_year_on_quarterly_means_latest_disclosed_annual():
    """线上踩过：停在时代新材 2026 三季报上点「年报体检」，规划器给了 period=FY、没给年份，
    沿用当前季报的年份拼出「2026 年报」（2027 年 4 月才披露）去巨潮找，报「没有找到」。
    体检现在留在季报上做合并体检；这条规则对「年报里…」这类普通问题照样要成立。"""
    asked = {"intent": "report", "target": {"company": "", "year": None, "period": "FY"}}
    assert _plan(asked, "2026", "Q3") == {"action": "switch", "doc": DOCS[0]}      # 精达 2025 年报
    # 本地没有那份年报：去巨潮导入的也是 2025 年报，不是 2026
    plan = plan_for(asked, current_company="精达股份", current_year="2026", current_period="Q3",
                    docs=DOCS[1:], today=TODAY)
    assert plan == {"action": "import", "company": "精达股份", "year": 2025, "period": "FY"}
    # 当前季报更早（2024 三季报）：同一年的年报已经披露，仍用 2024
    plan = plan_for(asked, current_company="精达股份", current_year="2024", current_period="Q3",
                    docs=[], today=TODAY)
    assert plan["year"] == 2024
    # 季报不受影响：在 2026 三季报上问「一季度呢」仍是 2026 一季报
    q1 = {"intent": "report", "target": {"company": "", "year": None, "period": "Q1"}}
    assert plan_for(q1, current_company="精达股份", current_year="2026", current_period="Q3",
                    docs=[], today=TODAY)["year"] == 2026


def test_clarify_and_out_of_scope_skip_resolution():
    assert _plan({"intent": "clarify", "target": {}}) == {"action": "clarify"}
    assert _plan({"intent": "out_of_scope", "target": {}}) == {"action": "out_of_scope"}


# ---------------------------------------------------------------- 收尾与存档

def test_quick_entry_and_memory_round_trip():
    turn = {"q": "对比一下", "doc_id": "0123456789abcdef", "understood": "反问澄清"}
    e = quick_entry(turn, "想比什么？", "clarify", ["2025 与 2024 年营收对比？", "毛利率对比？"])
    assert e["kind"] == "clarify" and e["options"][1] == "毛利率对比？" and e["hits"] == []
    back = memory.clean_entry(memory.pack([e])[0], {"0123456789abcdef"})
    assert back["kind"] == "clarify" and back["options"] == e["options"]
    assert back["understood"] == "反问澄清"
    refuse = memory.clean_entry(dict(e, kind="refuse", options=None), set())
    assert refuse["kind"] == "refuse" and "options" not in refuse


@pytest.mark.parametrize("kind", ["evil", "<script>"])
def test_unknown_kind_dropped(kind):
    e = quick_entry({"q": "x"}, "y", "clarify")
    assert "kind" not in memory.clean_entry(dict(e, kind=kind), set())
