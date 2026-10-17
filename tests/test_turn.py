"""回合状态机的测试。

这段逻辑原来内联在 app.py 里 —— `grep advance_turn tests/` 没有任何结果。
它跨 rerun 传状态、中间调两次真实模型，是仓库里最复杂、也是唯一没有被自动化覆盖的一块。

抽成 `ui/turn.py` 的纯核心之后，每条分支都能用假模型走一遍：
正常回答转扩展、弃答直接收尾、生成失败、扩展失败、无补充、历史透传。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.generator import ABSTAIN_MARK, EXTENSION_HEADING  # noqa: E402
from ui.turn import adopt_turn, history_entry, step  # noqa: E402

TURN = {"q": "毛利率为什么变", "status": "generating", "doc_id": "d1",
        "hits": [{"text": "x"}], "queries": ["q1"], "counted": False}
FIRST = "2025年营业收入 246.74 亿元（24,673,670,269.16元）[1]。"
KW = dict(client=object(), company="精达股份", year="2025")


def gen(text=FIRST, record=None):
    def _g(q, hits, *, client, company, year, history):
        if record is not None:
            record["gen"] = history
        return text
    return _g


def ana(text="成本增速慢于收入[1]。", diag=None, record=None):
    def _a(q, hits, first, *, client, company, year, history):
        if record is not None:
            record["ana"] = history
            record["first"] = first
        return text, dict(diag if diag is not None else {"analyze": "ok"})
    return _a


def boom(*a, **k):
    raise TimeoutError("timeout")


def run(turn=None, **kw):
    args = dict(gen=gen(), ana=ana())
    args.update(kw)
    return step(turn if turn is not None else dict(TURN), **KW, **args)


# ---------------------------------------------------------------- 分支

def test_normal_answer_moves_to_analyzing():
    turn, entry, error = run()
    assert error is None and entry is None
    assert turn["status"] == "analyzing"
    assert turn["a"] == FIRST


def test_abstention_finishes_without_a_second_call():
    """弃答不做扩展：没有结论可解释，问「为什么」只会诱发编造。"""
    called = {}
    turn, entry, error = run(
        gen=gen(text=f"{ABSTAIN_MARK}，片段里没有原因表述。"),
        ana=lambda *a, **k: called.setdefault("hit", True))
    assert error is None and "hit" not in called
    assert turn["status"] == "done" and entry["a"].strip()
    assert entry["analyze"] == ""


def test_generation_error_returns_error_and_no_entry():
    turn, entry, error = run(gen=boom)
    assert entry is None and error and "调用大模型失败" in error


def test_analysis_ok_appends_the_extension():
    _turn, entry, error = run(
        turn=dict(TURN, status="analyzing", a=FIRST),
        ana=ana(text="成本增速慢于收入[1]。"))
    assert error is None and entry["analyze"] == "ok"
    assert entry["a"] == f"{FIRST}\n\n{EXTENSION_HEADING}\n成本增速慢于收入[1]。"


def test_analysis_failure_keeps_the_first_answer():
    """首轮回答本身是完整的，不能因为更晚的一次补充调用而作废它。"""
    _turn, entry, error = run(turn=dict(TURN, status="analyzing", a=FIRST), ana=boom)
    assert error is None                      # 不算整轮失败
    assert entry["a"] == FIRST               # 原样保留，没有悬空的标题
    assert entry["analyze"] == "error"
    # 失败原因必须留下来 —— 回合对象一清，只剩一个 "error" 就查不出为什么了
    assert "timeout" in entry["analyze_error"]


def test_no_addition_keeps_the_first_answer():
    _turn, entry, error = run(turn=dict(TURN, status="analyzing", a=FIRST),
                              ana=ana(text="", diag={"analyze": "no_add"}))
    assert entry["a"] == FIRST and entry["analyze"] == "no_add"


def test_unknown_status_is_reported():
    _turn, entry, error = run(turn=dict(TURN, status="weird"))
    assert entry is None and error and "未知的回合状态" in error


# ---------------------------------------------------------------- 不变量

def test_recent_history_reaches_both_calls():
    """追问修复的回归：两个调用都必须拿到最近几轮的问题。"""
    rec = {}
    _t, _e, _err = run(turn=dict(TURN, status="analyzing", a=FIRST),
                       ana=ana(record=rec), recent=["营业收入是多少？"])
    assert rec["ana"] == ["营业收入是多少？"]
    assert rec["first"] == FIRST             # 扩展拿到的必须是首轮回答
    _t, _e, _err = run(gen=gen(record=rec), recent=["营业收入是多少？"])
    assert rec["gen"] == ["营业收入是多少？"]


def test_step_does_not_mutate_the_input_turn():
    """跨 rerun 的会话状态最容易出的错是「就地改了但忘了写回」。"""
    turn = dict(TURN)
    before = dict(turn)
    step(turn, **KW, gen=gen(), ana=ana())
    assert turn == before


def test_history_entry_shape():
    e = history_entry(dict(TURN, analyze="ok"), "回答")
    assert set(e) == {"q", "a", "hits", "queries", "analyze"}
    assert e["a"] == "回答" and e["analyze"] == "ok"


def test_history_entry_keeps_the_failure_reason():
    e = history_entry(dict(TURN, analyze="error", analyze_error="timeout"), "回答")
    assert e["analyze_error"] == "timeout"


def test_history_entry_defaults_analyze_when_absent():
    assert history_entry(TURN, "回答")["analyze"] == ""


# ---------------------------------------------------------------- 切换年报

def test_same_document_keeps_the_turn():
    turn = dict(TURN)
    kept, orphan = adopt_turn(turn, "d1")
    assert kept is turn and orphan is None


def test_switching_documents_drops_a_turn_without_an_answer():
    kept, orphan = adopt_turn(dict(TURN), "d2")
    assert kept is None and orphan is None          # 还没有首轮回答，没什么可留


def test_switching_during_analysis_keeps_the_first_answer():
    """扩展分析阶段用户已经看到首轮回答了 —— 切文档不该让眼前的答案凭空消失。

    这个窗口是加扩展分析之后才变宽的：原来只在「生成中」丢弃，那时用户什么都还没看到。
    """
    kept, orphan = adopt_turn(dict(TURN, status="analyzing", a=FIRST), "d2")
    assert kept is None
    old_id, entry = orphan
    assert old_id == "d1" and entry["a"] == FIRST
    assert entry["analyze"] == "aborted"


def test_no_turn_is_a_no_op():
    assert adopt_turn(None, "d1") == (None, None)
