"""回答风格（通俗 / 专业）：提示词切换、回滚开关、风格跟着回合走、术语表。"""
import re

from rag import config, generator
from ui import glossary, memory, payload
from ui.turn import history_entry


def test_pro_prompt_is_the_original():
    """专业版就是改版前的提示词，一个字不差 —— 回滚靠的就是这个。"""
    desc = "（精达股份 2025 年年度报告）"
    assert generator.system_prompt(desc, "pro") == generator.SYSTEM_PROMPT.format(doc_desc=desc)


def test_plain_prompt_keeps_rules_1_to_7():
    """通俗版只换第 8 条版式，准确性约束（1–7 条）原样保留。"""
    desc = "（x）"
    plain, pro = generator.system_prompt(desc, "plain"), generator.system_prompt(desc, "pro")
    head = pro[:pro.index("8. 使用中文")]
    assert plain.startswith(head)
    assert "这意味着什么" in plain and "需要留意" in plain
    assert "年报中未找到相关信息" in plain         # 弃答固定短语还在（is_abstention 靠它）
    assert "单位写在每个格子里" in plain           # 试跑：单位挪到表头，逐格核对认不出


def test_build_messages_uses_style():
    msgs = generator.build_messages("营收多少", [], style="plain")
    assert "这意味着什么" in msgs[0]["content"]
    msgs = generator.build_messages("营收多少", [])
    assert "这意味着什么" not in msgs[0]["content"]


def test_resolve_style(monkeypatch):
    monkeypatch.setattr(config, "ANSWER_STYLE", "plain")
    assert generator.resolve_style(None) == "plain"
    assert generator.resolve_style("pro") == "pro"
    assert generator.resolve_style("<script>") == "plain"
    # 回滚开关：界面传什么都按专业版
    monkeypatch.setattr(config, "ANSWER_STYLE", "pro")
    assert generator.resolve_style("plain") == "pro"
    assert generator.resolve_style(None) == "pro"


def _turn(**kw):
    return dict({"q": "营收多少", "hits": [], "queries": []}, **kw)


def test_history_entry_keeps_style_only_for_answers():
    assert history_entry(_turn(style="plain"), "x")["style"] == "plain"
    assert "style" not in history_entry(_turn(), "x")                       # 旧回合：按专业版显示
    assert "style" not in history_entry(_turn(style="plain", kind="health"), "x")  # 程序写的正文


def test_style_reaches_component_and_survives_refresh():
    entry = history_entry(_turn(style="plain"), "营收 246.74 亿元")
    assert payload.ai_message(entry)["style"] == "plain"
    packed = memory.pack([entry])
    back = memory.clean_entry(packed[0], set())
    assert back["style"] == "plain"
    assert "style" not in memory.clean_entry(dict(packed[0], style="evil"), set())


def test_build_data_switch_flag():
    on = payload.build_data(doc={}, messages=[], style_switch=True)
    off = payload.build_data(doc={}, messages=[], style_switch=False)
    assert on["styleSwitch"] and on["styleDefault"] == "plain" and on["glossary"]
    assert not off["styleSwitch"] and off["styleDefault"] == "pro" and off["glossary"] == []


def test_glossary_entries_are_well_formed():
    rows = glossary.payload()
    names = [r["t"] for r in rows]
    assert len(names) == len(set(names))
    seen = {}
    for r in rows:
        assert r["a"][0] == r["t"]
        assert r["d"] and r["w"]
        assert not re.search(r"<|>", r["d"] + r["w"])   # 说明是纯文字
        for a in r["a"]:
            assert a not in seen, f"{a} 同时属于 {seen.get(a)} 和 {r['t']}"
            seen[a] = r["t"]
