"""agent2 复核器：回答生成之后、交给用户之前，查它有没有答到点子上、有没有超出片段。

两层：
  1. 程序检查（rag/fidelity.check 等）—— 零成本、结果确定。但「数字在片段里找不到」不直接判死：
     评测里见过它把合法的换算、四舍五入算成无出处（tools/eval_rubric.py 的说明）。
     所以这一层只产出**线索**，交给第二层判断。
  2. 模型按清单逐项回答：每个要点答到没有、哪些说法片段里找不到、片段里明明有却说没找到。
     只为这三类问题判不通过，**不为措辞、格式、篇幅判不通过** —— 回退一次要多花 20 秒，
     只有答错、漏答才值得。

决定怎么回退的是 decide()：纯函数，最多回退一次、受总时长限制。模型给什么都过这一道。
"""
from __future__ import annotations

import json
import re

from . import config
from .fidelity import check as fidelity_check

VERDICTS = ("pass", "retrieve", "regenerate", "replan")
_CITE_RE = re.compile(r"\[(\d+)\]")
_SNIPPET_CHARS = 700          # 每个片段给复核模型看多少字：够判断，又不至于让复核比生成还慢

PROMPT = """你是上市公司定期报告问答的复核员。给你【问题】【问题理解】【报告片段】【回答】，以及程序发现的线索。
逐项判断这份回答能不能交给用户，只输出 JSON。

只看三件事：
1. 要点覆盖：【问题理解】里的每个要点，回答是否给出了答案；片段里确实没有时，回答明确说明"未找到/未披露"也算答到。
2. 有无依据：回答里的数字和事实能否在片段里找到，或由片段数字算出（换算单位、四舍五入、求差、求比例都算有依据）。
3. 漏看：回答说"未找到"，但片段里其实有。

不要因为措辞、格式、篇幅、语气、详略不通过。拿不准时判通过。

verdict 四选一：
- pass：三件事都没问题。
- regenerate：片段里有答案，但回答漏答、答错、或说了片段里没有的内容 —— 重新生成就能改好。
- retrieve：某个要点片段里确实没有，可能在报告的其他位置 —— 在 queries 里给 2–3 条新的检索短语（用报告原文用语）。
- replan：回答答的根本不是用户问的（公司、年份、报告期或问题理解错了）。

只输出 JSON：
{"points": [{"point": "要点", "ok": true}], "unsupported": ["找不到依据的说法，摘原句，最多 3 条"],
"missed": false, "verdict": "pass", "queries": [], "note": "不通过时用一句话说明问题，给用户看，不超过 40 字"}"""


def cited_out_of_range(answer: str, n_hits: int) -> list[int]:
    """回答里引用了不存在的片段编号（[9] 但只检索到 8 段）。"""
    return sorted({int(m) for m in _CITE_RE.findall(answer or "") if int(m) < 1 or int(m) > n_hits})


def program_checks(answer: str, hits: list[dict]) -> dict:
    """程序层：只给线索，不下结论（见模块说明）。"""
    fid = fidelity_check(answer or "", hits or [])
    return {
        "orphan_numbers": fid.get("无出处样例") or [],
        "orphan_percents": fid.get("无出处百分比", 0),
        "bad_cites": cited_out_of_range(answer, len(hits or [])),
    }


def _snippets(hits: list[dict]) -> str:
    rows = []
    for i, h in enumerate(hits or [], start=1):
        head = h.get("context_header") or ""
        text = (h.get("text") or "")[:_SNIPPET_CHARS]
        rows.append(f"[{i}] {head}\n{text}".strip())
    return "\n\n".join(rows) or "（没有检索到片段）"


def _user_message(question: str, plan: dict | None, answer: str, hits: list[dict],
                  clues: dict) -> str:
    plan = plan or {}
    points = plan.get("subquestions") or [question]
    rows = [f"【问题】{question}",
            "【问题理解】" + (plan.get("restated") or question) + "\n要点：" + "；".join(points),
            f"【报告片段】\n{_snippets(hits)}",
            f"【回答】\n{answer}"]
    hints = []
    if clues.get("orphan_numbers"):
        hints.append("这些数字程序没在片段里直接找到（可能是合法换算，也可能是编造，请你判断）："
                     + "、".join(clues["orphan_numbers"]))
    if clues.get("bad_cites"):
        hints.append("回答引用了不存在的片段编号：" + "、".join(f"[{n}]" for n in clues["bad_cites"]))
    if hints:
        rows.append("【程序线索】\n" + "\n".join(hints))
    return "\n\n".join(rows)


def normalize(data, clues: dict) -> dict | None:
    """模型输出 → 校验过的复核结果。不成形返回 None（调用方当作「复核没跑成」，不拦回答）。"""
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    points = []
    for p in data.get("points") or []:
        if isinstance(p, dict) and str(p.get("point") or "").strip():
            points.append({"point": str(p["point"]).strip()[:60], "ok": bool(p.get("ok", True))})
    unsupported = [str(x).strip()[:120] for x in (data.get("unsupported") or [])
                   if isinstance(x, str) and x.strip()][:3]
    queries = [str(x).strip()[:60] for x in (data.get("queries") or [])
               if isinstance(x, str) and x.strip()][:3]
    verdict = data["verdict"]
    # 引用了不存在的片段：确定的错误，模型漏判也要重新生成
    if clues.get("bad_cites") and verdict == "pass":
        verdict = "regenerate"
    # 说要补检索却没给检索词：退化成重新生成
    if verdict == "retrieve" and not queries:
        verdict = "regenerate"
    note = str(data.get("note") or "").strip()[:60]
    if verdict != "pass" and not note:
        missed = [p["point"] for p in points if not p["ok"]]
        note = ("没答到：" + "、".join(missed[:2])) if missed else "部分说法在片段里找不到依据"
    return {"verdict": verdict, "points": points, "unsupported": unsupported,
            "missed": bool(data.get("missed")), "queries": queries, "note": note}


def review(question: str, answer: str, hits: list[dict], *, client, plan: dict | None = None) -> dict | None:
    """一次复核。任何失败返回 None —— 复核是把关，不是关卡：它坏了不能拦住回答。"""
    clues = program_checks(answer, hits)
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": _user_message(question, plan, answer, hits, clues)}],
            temperature=0, max_tokens=config.REVIEW_MAX_TOKENS, timeout=config.REVIEW_TIMEOUT,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "")
    except Exception:
        return None
    out = normalize(data, clues)
    if out is not None:
        out["clues"] = clues
    return out


def decide(result: dict | None, *, round_no: int, elapsed: float,
           max_retry: int | None = None, budget: float | None = None) -> str:
    """复核结果 → 下一步：pass / retrieve / regenerate / replan / accept。

    accept = 没通过但不再重试，照常给出回答并标出问题。三种情况会走到这里：
    已经回退过 max_retry 次；剩下的时间不够再跑一轮（重试一轮约 25 秒）；复核本身没跑成时
    返回 pass（复核坏了不拦回答）。
    """
    max_retry = config.AGENT_MAX_RETRY if max_retry is None else max_retry
    budget = config.AGENT_TIME_BUDGET if budget is None else budget
    if result is None or result["verdict"] == "pass":
        return "pass"
    if round_no >= max_retry or elapsed + 25 > budget:
        return "accept"
    return result["verdict"]


def feedback_block(result: dict | None) -> str:
    """重新生成时告诉模型上一版错在哪。"""
    if not result:
        return ""
    rows = [f"上一版回答被复核指出：{result.get('note') or '有问题'}"]
    missed = [p["point"] for p in result.get("points") or [] if not p.get("ok")]
    if missed:
        rows.append("没答到的要点：" + "；".join(missed))
    if result.get("unsupported"):
        rows.append("片段里找不到依据、不要再写的说法：" + "；".join(result["unsupported"]))
    if result.get("missed"):
        rows.append("上一版说未找到，但片段里其实有，请仔细查看片段。")
    return "【复核意见】这一版请改正：\n" + "\n".join(rows) + "\n\n"


def summary(result: dict | None, rounds: int, verdict: str) -> dict:
    """存进历史、交给界面的复核结论（小而稳定的形状）。verdict：pass / fail / error。"""
    out = {"verdict": verdict, "rounds": rounds}
    if result:
        out["points"] = [{"point": p["point"], "ok": p["ok"]} for p in result.get("points") or []][:6]
        if verdict == "fail":
            out["note"] = result.get("note") or ""
            out["unsupported"] = list(result.get("unsupported") or [])[:3]
    return out
