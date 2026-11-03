"""按要点分别检索（不调模型）：规划器的要点检索词、名额分配、补检索留旧片段、轨迹那一行。

一问多项时所有检索词融合成一个排名，最好找的那一项占满名额、其余一段都分不到。
效果用 tools/eval_e2e.py 的一问多项题拿真实模型测；这里测的是分配规则本身。
"""
from datetime import date

import numpy as np

from rag import config, planner
from rag.retriever import Retriever, select_by_points
from ui import memory
from ui.payload import ai_message
from ui.turn import answered_hits, history_entry

TODAY = date(2026, 11, 3)


def _plan(**kw):
    base = {"restated": "", "intent": "report", "company": "", "multiple": False, "year": None,
            "period": None, "card": False, "subquestions": [], "queries": [], "point_queries": [],
            "clarify": {"question": "", "options": []}, "reply": ""}
    base.update(kw)
    return planner.normalize(base, "研发投入、前五大客户占比和分红方案分别是什么", [], TODAY)


# ---------------------------------------------------------------- 规划器

def test_points_follow_subquestions_in_order():
    out = _plan(subquestions=["研发投入", "前五大客户占比", "分红方案"],
                point_queries=[["研发投入金额"], ["前五名客户销售额占比"], ["利润分配预案", "现金红利"]])
    assert out["points"] == [{"point": "研发投入", "queries": ["研发投入金额"]},
                             {"point": "前五大客户占比", "queries": ["前五名客户销售额占比"]},
                             {"point": "分红方案", "queries": ["利润分配预案", "现金红利"]}]


def test_single_point_or_missing_queries_gives_no_points():
    assert _plan(subquestions=["营业收入"], point_queries=[["营业收入"]])["points"] == []
    # 只有一个要点有检索词：谈不上「分别」
    assert _plan(subquestions=["研发投入", "分红方案"], point_queries=[["研发投入金额"], []])["points"] == []
    assert _plan(subquestions=["a", "b"], point_queries="乱写")["points"] == []
    # 反问、超范围不检索
    assert _plan(intent="out_of_scope", subquestions=["a", "b"], point_queries=[["x"], ["y"]])["points"] == []


def test_fallback_has_no_points():
    assert planner.fallback("营收多少", client=None)["points"] == []


# ---------------------------------------------------------------- 名额分配

def _fill(rest, k):
    return sorted(rest, key=rest.get, reverse=True)[:k]


def test_each_point_gets_its_share_even_when_one_dominates():
    """融合排名里第一个要点的片段全排在前面；按要点分配后第二个要点照样分到 3 段。"""
    fused = {i: 1.0 - i * 0.01 for i in range(20)}
    per = [{i: 1.0 - i * 0.01 for i in range(10)},          # 要点一：0–9
           {i: 1.0 - i * 0.01 for i in range(10, 20)}]      # 要点二：10–19
    owner = select_by_points(per, fused, 8, 3, _fill)
    assert sorted(i for i, o in owner.items() if o == 1) == [10, 11, 12]
    assert sorted(i for i, o in owner.items() if o == 0) == [0, 1, 2]
    assert len(owner) == 8 and list(owner.values()).count(None) == 2


def test_overlapping_points_take_turns():
    """两个要点的头几名重叠时轮流取：后面那个要点不会只剩别人挑剩的。"""
    per = [{1: 3, 2: 2, 3: 1}, {1: 3, 4: 2, 5: 1}]
    owner = select_by_points(per, {}, 6, 2, _fill)
    assert owner == {1: 0, 4: 1, 2: 0, 5: 1}


def test_total_never_exceeds_k():
    per = [{i: 1 for i in range(5)}, {i: 1 for i in range(5, 10)}, {i: 1 for i in range(10, 15)}]
    assert len(select_by_points(per, {i: 1 for i in range(15)}, 4, 3, _fill)) == 4


def _chunks():
    texts = ["研发投入 金额 1.21 亿元"] * 6 + ["前五名客户 销售额 占比 9.69%"] * 3 + ["利润分配 现金红利"] * 3
    return [{"chunk_id": i, "section": f"s{i}", "subsection": "", "text": t, "index_text": t,
             "page_start": i + 1, "page_end": i + 1} for i, t in enumerate(texts)]


def _retriever():
    chunks = _chunks()
    return Retriever(chunks, np.zeros((len(chunks), 4), dtype="float32"),
                     embed=lambda ts: np.zeros((len(ts), 4), dtype="float32"))


POINTS = [{"point": "研发投入", "queries": ["研发投入"]},
          {"point": "前五大客户", "queries": ["前五名客户 销售额"]},
          {"point": "分红", "queries": ["利润分配 现金红利"]}]


def test_retriever_tags_hits_and_reports_counts():
    diag: dict = {}
    hits, queries = _retriever().search_with_queries("q", queries=["研发投入"], points=POINTS, diag=diag)
    assert len(hits) == 9                                  # 3 个要点 × 3 段，未超过上限 12
    assert diag["points"] == [{"point": "研发投入", "n": 3}, {"point": "前五大客户", "n": 3}, {"point": "分红", "n": 3}]
    assert {h["point"] for h in hits} == {"研发投入", "前五大客户", "分红"}
    assert "利润分配 现金红利" in queries


def test_switch_off_restores_single_ranking(monkeypatch):
    monkeypatch.setattr(config, "RETRIEVE_BY_POINTS", False)
    diag: dict = {}
    hits, _ = _retriever().search_with_queries("q", queries=["研发投入"], points=POINTS, diag=diag)
    assert "points" not in diag and not any("point" in h for h in hits)


# ---------------------------------------------------------------- 补检索、界面

def test_answered_hits_keeps_only_answered_points():
    hits = [{"text": "a", "point": "研发投入"}, {"text": "b", "point": "前五大客户占比"},
            {"text": "c"}, {"text": "d", "point": "研发投入", "note": True}]
    review = {"points": [{"point": "研发投入金额", "ok": True}, {"point": "前五名客户", "ok": False}]}
    assert answered_hits(hits, review, 8) == [{"text": "a", "point": "研发投入"}]
    assert answered_hits(hits, {"points": []}, 8) == []
    assert answered_hits(hits, review, 0) == []


def test_trace_line_survives_entry_archive_and_payload():
    turn = {"q": "q", "hits": [], "queries": [], "by_points": [{"point": "研发投入", "n": 3}, {"point": "分红", "n": 2}]}
    entry = history_entry(turn, "回答")
    assert entry["by_points"] == turn["by_points"]
    assert ai_message(entry)["byPoints"] == [{"point": "研发投入", "n": 3}, {"point": "分红", "n": 2}]
    packed = memory.pack([entry])[0]
    assert memory.clean_entry(packed, set())["by_points"] == turn["by_points"]
    # 浏览器送回来的是访客可控输入：形状不对的丢掉
    bad = memory.clean_entry(dict(packed, by_points=[{"point": "x", "n": 99}, "乱写"]), set())
    assert "by_points" not in bad
