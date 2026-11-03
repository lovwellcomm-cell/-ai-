"""agent2 复核器：回答生成之后、交给用户之前，查它有没有答到点子上、有没有超出片段。

两层：
  1. 程序检查（rag/fidelity.check 等）—— 零成本、结果确定。但「数字在片段里找不到」不直接判死：
     评测里见过它把合法的换算、四舍五入算成无出处（tools/eval_rubric.py 的说明）。
     所以这一层只产出**线索**，交给第二层判断。
  2. 模型按清单逐项回答：每个要点答到没有、哪些说法片段里找不到、片段里明明有却说没找到。
     只为这三类问题判不通过，**不为措辞、格式、篇幅判不通过** —— 回退一次要多花 20 秒，
     只有答错、漏答才值得。

决定怎么回退的是 decide()：纯函数，最多回退一次、受总时长限制。模型给什么都过这一道。

跨报告（第三期）：程序另查「跨口径计算」（rag/caliber.py），一定错的直接判重新生成；
补检索可以指定去别的报告找（report），由调用方在那份报告上检索、把原文片段接上。
"""
from __future__ import annotations

import json
import re

from . import config
from .caliber import cross_span
from .fidelity import check as fidelity_check
from .generator import is_abstention

VERDICTS = ("pass", "retrieve", "regenerate", "replan")
# 定期报告的常规披露项：问这些却答「未找到」，只可能是检索漏了
_ROUTINE_RE = re.compile(r"营收|收入|利润|净利|现金流|资产|负债|毛利|分红|利润分配|派息|研发|客户|供应商|员工|股东")
_CITE_RE = re.compile(r"\[(\d+)\]")
_SNIPPET_CHARS = 700          # 每个片段给复核模型看多少字：够判断，又不至于让复核比生成还慢

PROMPT = """你是上市公司定期报告问答的复核员。给你【问题】【问题理解】【报告片段】【回答】，以及程序发现的线索。
逐项判断这份回答能不能交给用户，只输出 JSON。

只看三件事：
1. 要点覆盖：【问题理解】里的每个要点，回答是否给出了答案；片段里确实没有时，回答明确说明"未找到/未披露"也算答到。
2. 有无依据：回答里的数字和事实能否在片段里找到，或由片段数字算出（换算单位、四舍五入、求差、求比例都算有依据）。
3. 漏看：回答说"未找到"，但片段里其实有。
4. 跨报告口径：片段来自不止一份报告时（片段开头写明「来自 xx」），回答要写明每个数出自哪份报告；
   口径不同的数（全年 vs 前三季度累计、单季 vs 累计）不能直接相减或算增长率 —— 这样算了判 regenerate。

回答说"未找到"时，先想这一项报告里通常有没有：营收、利润、现金流、资产负债、毛利率、分红、研发投入、
前五大客户和供应商、员工人数这类是定期报告的常规披露项，一定在报告里 —— 片段里没有就判 retrieve 并给检索词，
不要判 pass。只有报告通常不披露的内容（具体合同细节、未来股价等），说"未找到"才算答到。

不要因为措辞、格式、篇幅、语气、详略不通过。拿不准时判通过。

verdict 四选一：
- pass：三件事都没问题。
- regenerate：片段里有答案，但回答漏答、答错、或说了片段里没有的内容 —— 重新生成就能改好。
- retrieve：某个要点片段里确实没有，可能在报告的其他位置 —— 在 queries 里给 2–3 条新的检索短语（用报告原文用语）。
  答案更可能在【可补充检索的报告】里的某一份时（比如问上半年的原因，而主报告是三季报），report 填那份报告的名称（原样照抄）；
  否则 report 留空，在主报告里找。
- replan：回答答的根本不是用户问的（公司、年份、报告期或问题理解错了）。

只输出 JSON：
{"points": [{"point": "要点", "ok": true}], "unsupported": ["找不到依据的说法，摘原句，最多 3 条"],
"missed": false, "verdict": "pass", "queries": [], "report": "", "note": "不通过时用一句话说明问题，给用户看，不超过 40 字"}"""


def cited_out_of_range(answer: str, n_hits: int) -> list[int]:
    """回答里引用了不存在的片段编号（[9] 但只检索到 8 段）。"""
    return sorted({int(m) for m in _CITE_RE.findall(answer or "") if int(m) < 1 or int(m) > n_hits})


def program_checks(answer: str, hits: list[dict], facts: list[dict] | None = None) -> dict:
    """程序层：多数只给线索，不下结论（见模块说明）。facts 是带口径的流量数（rag/caliber.facts）。"""
    fid = fidelity_check(answer or "", hits or [])
    return {
        "orphan_numbers": fid.get("无出处样例") or [],
        "orphan_percents": fid.get("无出处百分比", 0),
        "bad_cites": cited_out_of_range(answer, len(hits or [])),
        "cross_span": cross_span(answer, hits or [], facts or []),
    }


def _snippets(hits: list[dict]) -> str:
    rows = []
    for i, h in enumerate(hits or [], start=1):
        head = h.get("context_header") or ""
        if h.get("note_src"):
            # 别的报告的片段（事实笔记、补充检索）：复核要知道它来自哪份，才查得了口径
            head = f"（来自 {h['note_src']}）\n{head}".strip()
        text = (h.get("text") or "")[:_SNIPPET_CHARS]
        rows.append(f"[{i}] {head}\n{text}".strip())
    return "\n\n".join(rows) or "（没有检索到片段）"


def _span_hint(c: dict) -> str:
    return f"{c['num']} 是由「{c['a']}」和「{c['b']}」算出的{c['how']}"


def _user_message(question: str, plan: dict | None, answer: str, hits: list[dict],
                  clues: dict, reports: list[str] | None = None) -> str:
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
    for c in clues.get("cross_span") or []:
        hints.append(_span_hint(c) + "：两个数口径不同"
                     + ("，这样算一定不对。" if c["hard"] else "；如果是在推算单季（全年减前三季度）可以，否则不对。"))
    if hints:
        rows.append("【程序线索】\n" + "\n".join(hints))
    if reports:
        rows.append("【可补充检索的报告】" + "、".join(reports))
    return "\n\n".join(rows)


def normalize(data, clues: dict, reports: list[str] | None = None) -> dict | None:
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
    # 跨口径算出的增长率、跨年差额：确定的错误（rag/caliber.py），模型漏判也要重新生成
    hard = [c for c in clues.get("cross_span") or [] if c["hard"]]
    if hard and verdict == "pass":
        verdict = "regenerate"
    # 说要补检索却没给检索词：退化成重新生成
    if verdict == "retrieve" and not queries:
        verdict = "regenerate"
    # 去别的报告补检索：只认给它看过的报告名，别的一律回到主报告
    report = str(data.get("report") or "").strip()
    report = report if verdict == "retrieve" and report in (reports or []) else ""
    note = str(data.get("note") or "").strip()[:60]
    if hard and (not note or data["verdict"] == "pass"):
        note = f"{hard[0]['num']} 是拿口径不同的两个数算的"
    if verdict != "pass" and not note:
        missed = [p["point"] for p in points if not p["ok"]]
        note = ("没答到：" + "、".join(missed[:2])) if missed else "部分说法在片段里找不到依据"
    return {"verdict": verdict, "points": points, "unsupported": unsupported,
            "missed": bool(data.get("missed")), "queries": queries, "report": report, "note": note}


def review(question: str, answer: str, hits: list[dict], *, client, plan: dict | None = None,
           facts: list[dict] | None = None, reports: list[str] | None = None) -> dict | None:
    """一次复核。任何失败返回 None —— 复核是把关，不是关卡：它坏了不能拦住回答。

    facts：带口径的流量数（rag/caliber.facts），查跨口径计算用；reports：可以去补检索的别的报告名。
    """
    clues = program_checks(answer, hits, facts)
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": _user_message(question, plan, answer, hits, clues, reports)}],
            temperature=0, max_tokens=config.REVIEW_MAX_TOKENS, timeout=config.REVIEW_TIMEOUT,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "")
    except Exception:
        data = None
    out = normalize(data, clues, reports)
    if out is None:
        # 复核模型坏了不拦回答 —— 除非程序已经查出一定错的跨口径计算（端到端评测里见过：模型出错，错的增长率照样交出去）
        if not any(c["hard"] for c in clues["cross_span"]):
            return None
        out = normalize({"verdict": "pass", "points": [], "note": ""}, clues)
    out["clues"] = clues
    # 常规披露项答成「未找到」：一定是检索没找全（评测里见过模型照样放行「时代新材营收多少 → 未找到」）
    if out["verdict"] == "pass" and is_abstention(answer) \
            and _ROUTINE_RE.search(question + " " + (plan or {}).get("restated", "")):
        out["verdict"] = "retrieve"
        out["queries"] = out["queries"] or ["主要会计数据和财务指标", "合并利润表 营业收入 净利润"]
        out["note"] = out["note"] or "报告里应有这项数据，回答却说未找到"
    return out


def decide(result: dict | None, *, round_no: int, elapsed: float, main_elapsed: float | None = None,
           max_retry: int | None = None, budget: float | None = None,
           turn_budget: float | None = None) -> str:
    """复核结果 → 下一步：pass / retrieve / regenerate / replan / accept。

    accept = 没通过但不再重试，照常给出回答并标出问题。走到这里的情况：
    已经回退过 max_retry 次；剩下的时间不够再跑一轮。复核本身没跑成时返回 pass（复核坏了不拦回答）。

    「够不够再跑一轮」按**第一轮实际用了多久**估：重答一轮和第一轮差不多久（检索 + 生成 + 复核）。
    最早按固定 25 秒估，端到端评测里趋势题第一轮 60 秒、照样放行回退，结果总共 117 秒，超了 90 秒上限。

    分步回答（先读了别的报告）时，第一轮只算主步骤（main_elapsed，从先读完开始）：回退不会再读一遍别的报告。
    原来按「已用时 ×2」算，先读用掉的 20–60 秒也翻倍，分步回答几乎从不回退；
    改成「已用时 + 主步骤用时 ≤ turn_budget（整轮上限）」。没有分步时和原来一样（已用时 ×2 ≤ budget）。
    """
    max_retry = config.AGENT_MAX_RETRY if max_retry is None else max_retry
    budget = config.AGENT_TIME_BUDGET if budget is None else budget
    turn_budget = config.AGENT_TURN_BUDGET if turn_budget is None else turn_budget
    if result is None or result["verdict"] == "pass":
        return "pass"
    if main_elapsed is None:
        over = elapsed * 2 > budget
    else:
        over = elapsed + main_elapsed > turn_budget
    if round_no >= max_retry or over:
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
    for c in (result.get("clues") or {}).get("cross_span") or []:
        if c["hard"]:
            rows.append(_span_hint(c) + "，口径不同不能这样比：删掉这个数，两个数分别列出并注明口径；"
                        "要比增长就用同口径的数（如上年同期的前三季度累计）")
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
