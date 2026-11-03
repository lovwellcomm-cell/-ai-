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
from ui.turn import (adopt_turn, conversation_key, history_entry,  # noqa: E402
                     plan_resolution, step)

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


def web(text="公开信息显示公司发布了回购公告[网1]。", sources=None, diag=None, record=None):
    def _w(q, first, *, client, company, year, history):
        if record is not None:
            record["web"] = history
            record["first"] = first
        return (text, list(sources if sources is not None
                           else [{"n": 1, "title": "公告", "url": "https://example.com/a"}]),
                dict(diag if diag is not None else {"web": "ok"}))
    return _w


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


def test_lookup_question_skips_the_analysis_call():
    """纯查数题不进 analyzing —— 省的是真实调用，不是延迟。

    实测 18 题里，题面带「是多少」的 6 题全部返回「无补充」。"""
    called = {}
    turn, entry, error = run(
        turn=dict(TURN, q="2025年营业收入是多少？"),
        ana=lambda *a, **k: called.setdefault("hit", True))
    assert error is None and "hit" not in called
    assert turn["status"] == "done" and entry["analyze"] == ""


def test_abstention_goes_to_web_search_when_available():
    """年报里没有的，才允许外部补充 —— 这是联网能力唯一的入口。"""
    rec = {}
    turn, entry, error = run(
        gen=gen(text=f"{ABSTAIN_MARK}，片段里没有相关信息。"), web=web(record=rec))
    # 第一跳只切状态，不在这里调用外部接口
    assert error is None and entry is None and turn["status"] == "searching"
    assert "web" not in rec
    turn, entry, error = run(turn=turn, web=web(record=rec))
    assert error is None and "web" in rec
    assert turn["status"] == "done"
    assert entry["web"] == "ok" and entry["web_sources"]
    assert "公开信息补充" in entry["a"]
    assert entry["a"].startswith(ABSTAIN_MARK)


def test_web_is_not_called_for_a_normal_answer():
    """年报答得出就不查外网 —— 否则引用就不再只指向年报。"""
    called = {}

    def w(*a, **k):
        called["hit"] = True

    turn, entry, error = run(web=w)
    assert "hit" not in called
    assert turn["status"] == "analyzing"


def test_web_failure_keeps_the_first_answer():
    def boom(*a, **k):
        raise TimeoutError("timeout")

    _t, entry, error = run(turn=dict(TURN, status="searching", a=FIRST), web=boom)
    assert error is None                      # 整轮不算失败
    assert entry["a"] == FIRST               # 首轮回答原样保留
    assert entry["web"] == "error"
    assert "timeout" in entry["web_error"]


def test_web_no_result_keeps_the_first_answer():
    _t, entry, error = run(turn=dict(TURN, status="searching", a=FIRST),
                           web=web(text="", diag={"web": "no_result"}))
    assert entry["a"] == FIRST and entry["web"] == "no_result"


def test_web_gets_history_and_the_first_answer():
    rec = {}
    run(turn=dict(TURN, status="searching", a=FIRST), web=web(record=rec),
        recent=["营业收入是多少？"])
    assert rec["web"] == ["营业收入是多少？"]
    assert rec["first"] == FIRST


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
    assert set(e) == {"q", "a", "hits", "queries", "analyze", "web", "web_sources", "doc_id"}
    assert e["doc_id"] == "d1"
    assert e["a"] == "回答" and e["analyze"] == "ok"
    assert e["web"] == "" and e["web_sources"] == []


def test_history_entry_keeps_the_failure_reason():
    e = history_entry(dict(TURN, analyze="error", analyze_error="timeout"), "回答")
    assert e["analyze_error"] == "timeout"


def test_history_entry_defaults_analyze_when_absent():
    assert history_entry(TURN, "回答")["analyze"] == ""


def test_history_entry_keeps_the_notice():
    e = history_entry(dict(TURN, notice="已自动导入《七匹狼 2025 年报》"), "回答")
    assert e["notice"] == "已自动导入《七匹狼 2025 年报》"


def test_history_entry_without_doc_id_keeps_old_shape():
    turn = {k: v for k, v in TURN.items() if k != "doc_id"}
    assert "doc_id" not in history_entry(turn, "回答")


def test_same_company_different_years_share_a_conversation():
    """七匹狼 2024 / 2025 同一段对话；「七匹狼股份」这种带后缀的写法也算同一家。"""
    a = conversation_key({"doc_id": "x1", "company": "七匹狼", "year": "2024"})
    b = conversation_key({"doc_id": "x2", "company": "七匹狼", "year": "2025"})
    c = conversation_key({"doc_id": "x3", "company": "七匹狼股份", "year": "2023"})
    assert a == b == c


def test_different_companies_and_unknown_company_stay_separate():
    assert conversation_key({"doc_id": "a", "company": "精达股份"}) != \
        conversation_key({"doc_id": "b", "company": "时代新材"})
    # 抽不出公司名的上传文档各自一段，不能全部挤进同一个「空公司」对话
    assert conversation_key({"doc_id": "u1", "company": ""}) != \
        conversation_key({"doc_id": "u2", "company": ""})


def test_history_entry_checks_answer_tables():
    """回答里有表格时，完成那一刻逐格核对一次并存下来；没有表格时不写这个键。"""
    turn = dict(TURN, hits=[{"text": "营业收入 24,673,670,269.16 22,322,579,930.98"}])
    table = ("结论[1]。\n\n| 项目 | 2025年 | 来源 |\n|---|---|---|\n"
             "| 营业收入 | 246.74 亿元 | [1] |\n| 编造 | 9.99 亿元 | [1] |\n")
    e = history_entry(turn, table)
    assert e["tableCheck"] == {"numbers": 2, "unverified": ["9.99 亿元"]}
    assert "tableCheck" not in history_entry(turn, "营业收入 246.74 亿元[1]。")


def test_history_entry_keeps_the_card():
    """指标卡挂在回合上；写历史时漏掉，回合一结束卡片就消失。"""
    card = {"kpis": [{"k": "营业收入", "v": "246.74", "u": "亿元"}],
            "charts": [{"title": "营业收入", "labels": ["2025"], "values": [246.74]}],
            "cardTitle": "主要会计数据 · 2025", "cardNote": "程序抽取 · 年报第 7 页 · 已与披露增减核对"}
    e = history_entry(dict(TURN, **card), "回答")
    for key, value in card.items():
        assert e[key] == value


def test_card_survives_through_generation_and_analysis_to_history():
    """生成 → 扩展分析 → 完成，两次状态转换之后卡片仍在。"""
    card = {"kpis": [{"k": "营业收入", "v": "1"}], "cardNote": "程序抽取"}
    mid, entry, err = step(dict(TURN, **card), **KW, gen=gen(), ana=ana())
    assert err is None and entry is None and mid["status"] == "analyzing"
    _, entry, err = step(mid, **KW, gen=gen(), ana=ana())
    assert err is None and entry is not None
    assert entry["kpis"] == card["kpis"] and entry["cardNote"] == "程序抽取"


# ---------------------------------------------------------------- 目标文档

DOCS = [
    {"doc_id": "a", "company": "精达股份", "year": "2025", "demo": True},
    {"doc_id": "b", "company": "七匹狼", "year": "2024", "demo": False},
    {"doc_id": "c", "company": "七匹狼", "year": "2025", "demo": False},
]


def plan(company="", year=None, **kw):
    return plan_resolution({"company": company, "year": year, "multiple": False},
                           current_company="精达股份", current_year="2025",
                           docs=DOCS, **kw)


def test_plan_uses_current_when_no_company_is_named():
    assert plan()["action"] == "current"


def test_plan_uses_current_for_the_same_company_without_a_year():
    assert plan("精达")["action"] == "current"


def test_plan_uses_current_for_the_same_company_and_year():
    assert plan("精达股份", 2025)["action"] == "current"


def test_plan_switches_to_the_local_doc_for_another_company():
    out = plan("七匹狼")
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "c"   # 取最新年度


def test_plan_switches_to_the_requested_year_when_local():
    out = plan("七匹狼", 2024)
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "b"


def test_plan_imports_when_the_company_is_not_local():
    out = plan("贵州茅台", 2025)
    assert out == {"action": "import", "company": "贵州茅台", "year": 2025, "period": "FY"}


def test_plan_treats_a_bare_year_followup_as_the_current_company():
    """「那2024年呢？」没有公司名，但年份变了 —— 应该是当前公司的 2024 年报。"""
    out = plan("", 2024)
    assert out == {"action": "import", "company": "精达股份", "year": 2024, "period": "FY"}


def test_plan_switches_for_a_bare_year_followup_when_local():
    docs = DOCS + [{"doc_id": "d", "company": "精达股份", "year": "2024"}]
    out = plan_resolution({"company": "", "year": 2024, "multiple": False},
                          current_company="精达股份", current_year="2025", docs=docs)
    assert out["action"] == "switch" and out["doc"]["doc_id"] == "d"


def test_plan_imports_another_year_of_the_current_company():
    out = plan("精达股份", 2024)
    assert out["action"] == "import"


def test_plan_rejects_multiple_companies():
    out = plan_resolution({"company": "七匹狼", "year": None, "multiple": True},
                          current_company="精达股份", current_year="2025", docs=DOCS)
    assert out["action"] == "multiple"


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


def test_switching_during_search_keeps_the_first_answer():
    """外部查询阶段用户也已经在看首轮回答了 —— 切文档同样要留下它。"""
    kept, orphan = adopt_turn(dict(TURN, status="searching", a=FIRST), "d2")
    assert kept is None
    old_id, entry = orphan
    assert old_id == "d1" and entry["a"] == FIRST
    assert entry["web"] == "aborted"


def test_no_turn_is_a_no_op():
    assert adopt_turn(None, "d1") == (None, None)


# ---------- 停止生成（ui/turn.stop_turn） ----------
from ui.turn import stop_turn  # noqa: E402
from ui.payload import ai_message  # noqa: E402


def test_stop_before_answer_keeps_nothing():
    assert stop_turn(None) is None
    assert stop_turn(dict(TURN)) is None                       # generating：还没出回答
    assert stop_turn({"q": "x", "status": "resolving"}) is None


def test_stop_during_analysis_keeps_first_answer_marked_stopped():
    entry = stop_turn(dict(TURN, status="analyzing", a=FIRST, started=0))
    assert entry["a"] == FIRST and entry["q"] == TURN["q"]
    assert entry["stopped"] is True and entry["analyze"] == "stopped"
    assert ai_message(entry)["stopped"] is True


def test_stop_during_search_marks_web():
    entry = stop_turn(dict(TURN, status="searching", a=FIRST))
    assert entry["web"] == "stopped" and entry["analyze"] == ""


def test_stop_during_review_drops_unfinished_review():
    # 复核没跑完：不带复核结论，界面不会标「已复核」
    entry = stop_turn(dict(TURN, status="reviewing", a=FIRST, review={"verdict": "pass"}))
    assert "review" not in entry and entry["stopped"] is True


def test_stop_tolerates_turn_without_hits():
    # 体检这类不检索的回合没有 hits / queries 键
    entry = stop_turn({"q": "体检", "status": "analyzing", "a": "正文", "kind": "health"})
    assert entry["hits"] == [] and entry["queries"] == [] and entry["kind"] == "health"


def test_finished_answer_is_not_marked_stopped():
    assert "stopped" not in ai_message(history_entry(dict(TURN), FIRST))
