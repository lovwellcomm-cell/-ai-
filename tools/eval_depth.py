"""离线测量：同一组问题在不同检索配置下的回答质量与耗时。

用途是"先实测再决定"，不是评测框架。它对不了标准答案 —— 年报问答没有公开的 gold set，
自动打分不可靠。所以脚本只做两件事：
  1. 把**可以程序化算的信号**固定下来（耗时、命中章节数、回答里的年份数、有没有同比、
     有没有答"未找到"、引用编号是否有效、token 用量）
  2. 把每道题的回答并排写成 markdown，交给人读

质量最终由人裁定；脚本负责让"改之前"和"改之后"可比。

用法：
    python tools/eval_depth.py                     # 跑当前配置
    python tools/eval_depth.py --label baseline    # 给这一轮起个名
    python tools/eval_depth.py --no-llm            # 不用大模型改写查询（省额度）
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，表格里的符号会直接抛 UnicodeEncodeError 打断整轮测量
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402

from rag import config  # noqa: E402
from rag.generator import cited_numbers, generate, get_client  # noqa: E402
from rag.indexer import load_index  # noqa: E402
from rag.retriever import Retriever  # noqa: E402

DEMO_ID = "2d3dee37263a663e"
OUT_DIR = ROOT / "eval_out"

# 覆盖用户报的三个缺口 + 一个速度基线。第一组是两问连发，第二问故意只说"那去年呢"。
QUESTIONS = [
    ("趋势", "近三年营业收入和归母净利润的变化趋势如何？"),
    ("覆盖", "毛利率变动的原因是什么？"),
    ("跨表", "经营活动产生的现金流量净额与净利润的差异说明了什么？"),
    ("简单", "2025年营业收入是多少？"),
]
FOLLOWUP = ("营业收入和归母净利润分别是多少？", "那去年呢？")

YEAR_RE = re.compile(r"20\d\d")
YOY_RE = re.compile(r"(?:同比|增长|下降|减少|增加|上升)[^。；\n]{0,12}?(\d+(?:\.\d+)?)\s*%")
ABSTAIN_RE = re.compile(r"未找到|没有找到|未披露|无法确定|没有相关")


def signal(answer: str, hits: list[dict]) -> dict:
    """全部是可程序化计算的信号，不需要人工标注。"""
    sections = {h.get("section") or "" for h in hits}
    cited = cited_numbers(answer, len(hits))
    n_sentences = max(1, len([s for s in re.split(r"[。；\n]", answer) if s.strip()]))
    n_with_cite = len([s for s in re.split(r"[。；\n]", answer) if re.search(r"\[\d", s)])
    return {
        "命中片段": len(hits),
        "命中章节数": len(sections),           # 多样性限流是否生效
        "带表头片段": sum(1 for h in hits if h.get("context_header")),
        "回答年份数": len(set(YEAR_RE.findall(answer))),
        "含同比": bool(YOY_RE.search(answer)),
        "答未找到": bool(ABSTAIN_RE.search(answer)),
        "引用编号": len(cited),
        "引用命中率": round(n_with_cite / n_sentences, 2),
        "回答字数": len(answer),
    }


def ask(retriever, question, client, history=None):
    diag: dict = {}
    t0 = time.perf_counter()
    hits, queries = retriever.search_with_queries(
        question, client=client, use_llm=config.LLM_REWRITE,
        # 只把**问题**传下去，不传回答：上一轮回答里可能有算错或臆测的数字
        history=[h["q"] for h in history] if history else None,
        diag=diag,
    )
    t1 = time.perf_counter()
    usage: dict = {}
    answer = "".join(generate(question, hits, client=client,
                              company="精达股份", year="2025", usage_out=usage))
    t2 = time.perf_counter()
    return {
        "question": question,
        "queries": queries,
        "hits": hits,
        "answer": answer,
        "retrieve_s": round(t1 - t0, 2),
        "generate_s": round(t2 - t1, 2),
        "total_s": round(t2 - t0, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "rewrite": diag.get("rewrite"),
        "rewrite_error": diag.get("rewrite_error"),
        "signal": signal(answer, hits),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="current")
    ap.add_argument("--no-llm", action="store_true", help="不使用大模型做查询改写")
    ap.add_argument("--doc", default=DEMO_ID)
    args = ap.parse_args()

    if args.no_llm:
        config.LLM_REWRITE = False

    meta, chunks, emb = load_index(args.doc)
    retriever = Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))
    client = get_client()
    print(f"年报：{meta.get('name')}（{meta.get('pages')} 页 / {len(chunks)} 片段）")
    print(f"配置：TOP_K={config.TOP_K} MAX_PER_SECTION={config.MAX_PER_SECTION} "
          f"LLM={config.LLM_MODEL} LLM_REWRITE={config.LLM_REWRITE}\n")

    runs = []
    for tag, q in QUESTIONS:
        print(f"[{tag}] {q}")
        r = ask(retriever, q, client)
        r["tag"] = tag
        runs.append(r)
        print(f"    {r['total_s']}s（检索 {r['retrieve_s']} / 生成 {r['generate_s']}）"
              f"  命中 {r['signal']['命中片段']} 片 / {r['signal']['命中章节数']} 节"
              f"  带表头 {r['signal']['带表头片段']}"
              f"  年份 {r['signal']['回答年份数']}  同比 {'有' if r['signal']['含同比'] else '无'}"
              f"  未找到 {'是' if r['signal']['答未找到'] else '否'}\n")

    print(f"[追问] {FOLLOWUP[0]}  →  {FOLLOWUP[1]}")
    r1 = ask(retriever, FOLLOWUP[0], client)
    r2 = ask(retriever, FOLLOWUP[1], client, history=[{"q": FOLLOWUP[0]}])
    for tag, r in (("追问-1", r1), ("追问-2", r2)):
        r["tag"] = tag
        runs.append(r)
    print(f"    第二问检索词：{r2['queries']}")
    print(f"    改写状态：{r2['rewrite']}"
          + (f"（{r2['rewrite_error']}）" if r2.get("rewrite_error") else ""))
    carried = any(("收入" in q or "利润" in q) for q in r2["queries"])
    print(f"    第二问是否接上第一问的主题：{'是' if carried else '否'}\n")

    OUT_DIR.mkdir(exist_ok=True)
    stamp = f"{args.label}-{time.strftime('%m%d-%H%M')}"

    rows = ["| 题目 | 耗时 | 检索 | 生成 | 检索词 | 改写 | 命中 | 章节 | 带表头 | 年份 | 同比 | 未找到 | 引用命中率 | tok |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        s = r["signal"]
        rows.append(
            f"| {r['tag']} | {r['total_s']}s | {r['retrieve_s']}s | {r['generate_s']}s | "
            f"{len(r['queries'])} 条 | {r['rewrite'] or '-'} | "
            f"{s['命中片段']} | {s['命中章节数']} | {s['带表头片段']} | {s['回答年份数']} | "
            f"{'是' if s['含同比'] else '否'} | {'是' if s['答未找到'] else '否'} | "
            f"{s['引用命中率']} | {r['completion_tokens']} |")
    table = "\n".join(rows)

    body = [f"# 深度测量 · {args.label} · {stamp}\n",
            f"- 年报：{meta.get('name')}（{meta.get('pages')} 页 / {len(chunks)} 片段）",
            f"- 配置：`TOP_K={config.TOP_K}` `MAX_PER_SECTION={config.MAX_PER_SECTION}` "
            f"`LLM={config.LLM_MODEL}` `LLM_REWRITE={config.LLM_REWRITE}`\n",
            "## 数字\n", table, "\n", "## 回答\n"]
    for r in runs:
        body.append(f"### [{r['tag']}] {r['question']}\n")
        body.append(f"- 耗时 {r['total_s']}s（检索 {r['retrieve_s']}s / 生成 {r['generate_s']}s）")
        body.append(f"- 检索词：{' ｜ '.join(r['queries'])}")
        body.append(f"- 命中：{', '.join(sorted({h.get('section', '') for h in r['hits']}))}\n")
        body.append(r["answer"] + "\n")

    (OUT_DIR / f"{stamp}.md").write_text("\n".join(body), encoding="utf-8")
    (OUT_DIR / f"{stamp}.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "hits"} |
                    {"hit_sections": [h.get("section") for h in r["hits"]],
                     "hit_subsection": [h.get("subsection") for h in r["hits"]]}
                    for r in runs], ensure_ascii=False, indent=2), encoding="utf-8")

    print(table)
    md = OUT_DIR / f"{stamp}.md"
    print(f"\n回答全文写入 {md.relative_to(ROOT)}（同目录另有 json）")


if __name__ == "__main__":
    main()
