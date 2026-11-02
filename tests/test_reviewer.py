"""agent2 复核器：输出校验、回退决策、和状态机的衔接（不调模型）。

复核模型判得准不准，用 tools/eval_review.py 拿真实回答和注入错误的回答测；
这里测的是「复核说什么都不会让流程失控」：最多回退一次、超时不回退、复核坏了不拦回答。
"""
import pytest

from rag import reviewer
from ui.turn import step

HITS = [{"text": "营业收入 24,673,670,269.16 22,322,579,930.98 10.53", "page_start": 7, "page_end": 7}]


def _res(verdict="pass", **kw):
    d = {"verdict": verdict, "points": [{"point": "营业收入", "ok": verdict == "pass"}],
         "unsupported": [], "missed": False, "queries": [], "note": ""}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 输出校验

def test_bad_output_is_none():
    assert reviewer.normalize({"verdict": "maybe"}, {}) is None
    assert reviewer.normalize([], {}) is None


def test_retrieve_without_queries_becomes_regenerate():
    out = reviewer.normalize(_res("retrieve"), {})
    assert out["verdict"] == "regenerate" and out["note"].startswith("没答到")


def test_bad_citation_forces_regenerate():
    out = reviewer.normalize(_res("pass"), {"bad_cites": [9]})
    assert out["verdict"] == "regenerate"


def test_program_checks_find_bad_cites_and_orphans():
    clues = reviewer.program_checks("营业收入 246.74 亿元[1]，另有 999.99 亿元[3]", HITS)
    assert clues["bad_cites"] == [3]
    assert any("999.99" in x for x in clues["orphan_numbers"])


# ---------------------------------------------------------------- 回退决策

@pytest.mark.parametrize("res, round_no, elapsed, want", [
    (None, 0, 1, "pass"),                                   # 复核没跑成：不拦回答
    (_res("pass"), 0, 1, "pass"),
    (_res("regenerate"), 0, 10, "regenerate"),
    (_res("retrieve", queries=["主要会计数据"]), 0, 10, "retrieve"),
    (_res("replan"), 0, 10, "replan"),
    (_res("regenerate"), 1, 10, "accept"),                  # 已经回退过一次
    (_res("regenerate"), 0, 40, "regenerate"),              # 第一轮 40 秒，再来一轮 80 秒，还在 90 以内
    (_res("regenerate"), 0, 60, "accept"),                  # 回归：第一轮 60 秒再重答一轮会到 120 秒
])
def test_decide(res, round_no, elapsed, want):
    assert reviewer.decide(res, round_no=round_no, elapsed=elapsed, max_retry=1, budget=90) == want


def test_feedback_and_summary():
    res = _res("regenerate", note="没答到净利润", unsupported=["毛利率提升因为提价"])
    block = reviewer.feedback_block(res)
    assert "没答到净利润" in block and "毛利率提升因为提价" in block
    s = reviewer.summary(res, 1, "fail")
    assert s == {"verdict": "fail", "rounds": 1, "points": [{"point": "营业收入", "ok": False}],
                 "note": "没答到净利润", "unsupported": ["毛利率提升因为提价"]}
    assert "note" not in reviewer.summary(_res(), 0, "pass")


# ---------------------------------------------------------------- 状态机

def _turn(**kw):
    t = {"q": "营业收入多少", "hits": HITS, "queries": [], "status": "generating", "started": 1000.0}
    t.update(kw)
    return t


def gen(q, hits, **kw):
    return "营业收入 246.74 亿元[1]。"


def ana(q, hits, first, **kw):
    return "扩展段", {"analyze": "ok"}


def run(turn, rev, now=1010.0, **kw):
    return step(turn, gen=gen, ana=ana, rev=rev, now=lambda: now, **kw)


def test_generating_goes_to_reviewing_first():
    """复核插在中间，复核完要去的地方和不复核时一样。"""
    plain, _, _ = step(_turn(), gen=gen, ana=ana)
    new, entry, err = run(_turn(), rev=lambda *a, **k: _res())
    assert new["status"] == "reviewing" and new["after_review"] == plain["status"] and entry is None


def test_pass_finishes_with_review_summary():
    t = _turn(status="reviewing", a="营业收入 246.74 亿元[1]。", after_review="done")
    new, entry, err = run(t, rev=lambda *a, **k: _res())
    assert entry["review"]["verdict"] == "pass" and "after_review" not in entry


def test_review_runs_alongside_analysis():
    t = _turn(status="reviewing", a="首轮", after_review="analyzing")
    new, entry, err = run(t, rev=lambda *a, **k: _res())
    assert entry["a"].startswith("首轮") and "扩展段" in entry["a"] and entry["analyze"] == "ok"


def test_regenerate_keeps_old_answer_and_counts_round():
    t = _turn(status="reviewing", a="旧回答", after_review="done")
    new, entry, err = run(t, rev=lambda *a, **k: _res("regenerate", note="漏了净利润"))
    assert (new["status"], new["round"], new["retry"], new["a"]) == ("generating", 1, "regenerate", "旧回答")
    assert entry is None and new["review_last"]["note"] == "漏了净利润"


def test_retrieve_carries_queries():
    t = _turn(status="reviewing", a="未找到", after_review="searching")
    new, _, _ = run(t, rev=lambda *a, **k: _res("retrieve", queries=["分红 利润分配"]))
    assert new["status"] == "retrieving" and new["retry_queries"] == ["分红 利润分配"]


def test_replan_drops_card():
    t = _turn(status="reviewing", a="x", after_review="done", kpis=[{"k": "营收"}], cardTitle="t")
    new, _, _ = run(t, rev=lambda *a, **k: _res("replan", note="问的是 2024 年"))
    assert new["status"] == "resolving" and "kpis" not in new and new["replan_hint"] == "问的是 2024 年"


def test_second_round_failure_is_accepted_not_retried():
    t = _turn(status="reviewing", a="还是漏了", after_review="done", round=1)
    new, entry, err = run(t, rev=lambda *a, **k: _res("regenerate", note="漏了净利润"))
    assert entry["review"]["verdict"] == "fail" and entry["review"]["rounds"] == 1


def test_reviewer_crash_does_not_block_answer():
    def boom(*a, **k):
        raise RuntimeError("超时")
    t = _turn(status="reviewing", a="回答", after_review="done")
    new, entry, err = run(t, rev=boom)
    assert err is None and entry["a"] == "回答" and entry["review"]["verdict"] == "error"


def test_pass_then_web_search_when_abstaining():
    t = _turn(status="reviewing", a="年报中未找到相关信息", after_review="searching")
    new, entry, _ = run(t, rev=lambda *a, **k: _res())
    assert new["status"] == "searching" and new["review"]["verdict"] == "pass"


def test_without_reviewer_flow_unchanged():
    new, entry, err = step(_turn(), gen=gen, ana=ana)
    assert new["status"] in ("done", "analyzing") and "after_review" not in new
    new, entry, err = step(new, gen=gen, ana=ana) if new["status"] != "done" else (new, entry, err)
    assert "review" not in entry


def test_empty_answer_retried_once_then_error():
    empty = lambda *a, **k: ""
    new, entry, err = step(_turn(), gen=empty, ana=ana)
    assert new["status"] == "generating" and new["empty_retry"] and err is None
    new, entry, err = step(new, gen=empty, ana=ana)
    assert err == "模型没有返回内容，请重试。"


class _FakeClient:
    """复核模型永远说 pass：用来测代码兜底。"""

    def __init__(self, content):
        msg = type("M", (), {"content": content})
        choice = type("C", (), {"message": msg})
        resp = type("R", (), {"choices": [choice]})
        self.chat = type("Chat", (), {"completions": type("Comp", (), {"create": lambda self, **k: resp})()})()


def test_routine_item_abstention_forced_to_retrieve():
    """评测里见过：「时代新材营收多少」答成「年报中未找到」，复核模型照样放行。"""
    client = _FakeClient('{"verdict": "pass", "points": [{"point": "营业收入", "ok": true}]}')
    out = reviewer.review("时代新材营收多少", "年报中未找到相关信息。", HITS, client=client)
    assert out["verdict"] == "retrieve" and out["queries"]
    # 报告通常不披露的东西，说未找到可以放行
    out = reviewer.review("明年股价目标是多少", "年报中未找到相关信息。", HITS, client=client)
    assert out["verdict"] == "pass"
