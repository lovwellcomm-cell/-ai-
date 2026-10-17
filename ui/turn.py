"""回合状态机的纯核心：生成 → 扩展 → 完成。

app.py 原本把「生成完转到哪、什么时候收尾、出错怎么办」全写在 Streamlit 脚本里。
那段逻辑要跨 rerun 传状态、中间调两次真实模型，**却一行自动化测试都没有** ——
grep `advance_turn` 在 tests/ 里没有任何结果。

把决策过程抽到这里之后，app.py 只负责接 session_state 和真实模型调用，
而"谁在什么时候转到哪个状态"可以用假的模型跑测试。
"""
from __future__ import annotations

from rag.generator import join_answer, should_analyze


def history_entry(turn: dict, answer: str) -> dict:
    """一轮完成时写进 history 的形状。

    `analyze` 只是排查用的状态（ok / no_add / duplicate / ungrounded / trimmed /
    truncated / error / skipped），组件不读它。失败原因一并留下 —— 只记一个 "error"
    而不记为什么，等于把排查线索丢在回合对象里、回合一清就没了。
    """
    entry = {
        "q": turn["q"],
        "a": answer,
        "hits": turn["hits"],
        "queries": turn["queries"],
        "analyze": turn.get("analyze", ""),
    }
    if turn.get("analyze_error"):
        entry["analyze_error"] = turn["analyze_error"]
    return entry


def adopt_turn(turn: dict | None, doc_id: str):
    """当前文档变了时，怎么处理进行中的回合。

    返回 `(保留的回合, (原文档 id, 要补写的历史条目) | None)`。

    切换年报必须丢弃进行中的回合，否则状态会串到另一份文档上。但**扩展分析阶段**
    首轮回答已经显示出来了 —— 用户正看着它，切一下文档它就凭空消失，比之前只在
    生成阶段丢弃更糟（那时候用户还没看到任何东西）。所以先把它落进**原来那份**
    文档的历史，只是没有扩展段，并记下 `analyze="aborted"`。
    """
    if not turn or turn.get("doc_id") == doc_id:
        return turn, None
    entry = None
    if turn.get("a"):
        entry = history_entry(dict(turn, analyze="aborted"), turn["a"])
    return None, ((turn.get("doc_id"), entry) if entry else None)


def step(turn: dict, *, gen, ana, client=None, company: str = "", year: str = "",
         recent: list[str] | None = None):
    """推进「生成 → 扩展 → 完成」这一段。

    参数：
      gen(question, hits, client=, company=, year=, history=) -> str
      ana(question, hits, first_answer, client=, company=, year=, history=) -> (str, dict)

    返回 `(new_turn, entry, error)`：

      · `error` 非 None → 失败。调用方退款并把错误挂到回合上。
      · `entry` 非 None → 这一轮完成。调用方写进 history 并清掉进行中的回合。
      · 两者皆 None   → 状态变了（generating → analyzing）。调用方存回 session_state 并 rerun。

    **不修改传入的 turn**，返回的是新字典。跨 rerun 的会话状态最容易出的错，
    就是"就地改了但忘了写回 session_state" —— 用不可变风格把这类错挡在函数外面。
    """
    status = turn.get("status")

    if status == "generating":
        try:
            answer = gen(turn["q"], turn["hits"], client=client,
                         company=company, year=year, history=recent)
        except Exception as e:
            return turn, None, f"调用大模型失败：{e}"

        new = dict(turn, a=answer)
        # 弃答不做扩展：没有结论可解释，此时问"为什么"只会诱发编造。
        if should_analyze(answer):
            return dict(new, status="analyzing"), None, None
        return dict(new, status="done"), history_entry(new, answer), None

    if status == "analyzing":
        diag: dict = {}
        ext = ""
        try:
            ext, diag = ana(turn["q"], turn["hits"], turn["a"], client=client,
                            company=company, year=year, history=recent)
        except Exception as e:
            # 首轮回答本身是完整的，不能因为更晚的一次**补充**调用而作废它。
            # 失败记在回合上（测量与排查用），用户拿到的仍是一份可用的回答。
            diag = {"analyze": "error", "analyze_error": str(e)}
            ext = ""
        new = dict(turn, analyze=diag.get("analyze", ""))
        if diag.get("analyze_error"):
            new["analyze_error"] = diag["analyze_error"]
        return (dict(new, status="done"),
                history_entry(new, join_answer(turn["a"], ext)), None)

    return turn, None, f"未知的回合状态：{status}"
