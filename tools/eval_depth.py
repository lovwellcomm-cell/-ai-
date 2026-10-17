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
from rag.generator import analyze, cited_numbers, generate, get_client, should_analyze  # noqa: E402
from rag.indexer import load_index  # noqa: E402
from rag.retriever import Retriever  # noqa: E402
from tools.eval_rubric import run as rubric_run  # noqa: E402

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
NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def abstained(answer: str) -> bool:
    """只把「真的答不出来」算作弃答。

    这里踩过坑：一开始用 `未找到|未披露|无法确定` 做匹配，结果把
    「年报片段未对毛利率变动原因作专门说明，但从片段可以看出…」这种
    **有数据、只是诚实标注了不确定性**的回答也判成了弃答，得出「两题从答未找到
    变成答出来了」的错误结论。所以现在要求：既出现明确的弃答措辞，
    又几乎没有任何数字，才算弃答。
    """
    explicit = "未找到相关信息" in answer or "年报中未找到" in answer
    return explicit and len(NUM_RE.findall(answer)) < 4


def extension_signals(ext: str, hits: list[dict], status: str = "") -> dict:
    """扩展段**单独**度量，不和首轮回答混在一起。

    混在一起会稀释信号：首轮那些规规矩矩的句子会把"扩展段到底有没有编造"这个
    真正要看的点冲淡掉 —— 而扩展段正是这个功能最可能出问题的地方。
    键永远存在（哪怕没有扩展），否则表格会出现空格。
    """
    ext = ext or ""
    if not ext:
        return {"扩展存在": False, "扩展状态": status, "扩展字数": 0,
                "扩展未披露": False, "扩展无出处": 0, "扩展有据可依率": None,
                "扩展无据归因数": 0, "扩展推断式归因数": 0, "扩展越界数": 0}
    r = rubric_run(ext, hits, "")
    return {
        "扩展存在": True,
        "扩展状态": status,
        "扩展字数": len(ext),
        "扩展未披露": "未披露" in ext,
        "扩展无出处": r["无出处"],
        "扩展有据可依率": r["有据可依率"],
        "扩展无据归因数": r["无据归因数"],
        "扩展推断式归因数": r["推断式归因数"],
        "扩展越界数": r["越界数"],
    }


def signal(answer: str, hits: list[dict], tag: str = "", extension: str = "",
           ext_status: str = "") -> dict:
    """全部是可程序化计算的信号，不需要人工标注。

    分两层：

    **台账层**（命中片段数、章节数、表头数、年份数、引用命中率…）——
    这些是「这一轮发生了什么」，是数字不是判断。注意 `引用命中率` 对「覆盖」那道题
    恒为 0.29，那只是 `分点依据：` 这行被当成了一个没有引用的句子，**不是质量信号**。

    **细则层**（`tools/eval_rubric.py`）—— 把「有深度的回答」那份 Word 细则
    逐条翻译成检查。其中「有据可依率」是最硬的一条：回答里的每个金额，
    要么能在**检索到的片段**里找到出处，要么能用片段里的数字按明确算式推出来。
    对财报工具来说这条最重要 —— 一个编造数字的"深度"回答比简短正确的更危险。
    """
    sections = {h.get("section") or "" for h in hits}
    cited = cited_numbers(answer, len(hits))
    numbers = NUM_RE.findall(answer)
    n_sentences = max(1, len([s for s in re.split(r"[。；\n]", answer) if s.strip()]))
    n_with_cite = len([s for s in re.split(r"[。；\n]", answer) if re.search(r"\[\d", s)])
    return {
        # 台账
        "命中片段": len(hits),
        "命中章节数": len(sections),           # 多样性限流是否生效
        "带表头片段": sum(1 for h in hits if h.get("context_header")),
        "回答年份数": len(set(YEAR_RE.findall(answer))),
        "含同比": bool(YOY_RE.search(answer)),
        "弃答": abstained(answer),
        "数字密度": round(len(numbers) * 100 / max(1, len(answer)), 1),
        "引用编号": len(cited),
        "引用命中率": round(n_with_cite / n_sentences, 2),
        "回答字数": len(answer),
        # 细则（含金额个数/有据可依率/无出处/无出处样例，那几条从 eval_fidelity 来）
        **rubric_run(answer, hits, tag),
        # 扩展分析单独量
        **extension_signals(extension, hits, ext_status),
    }


# ---------- 表格排版 ----------
# 列只在这里定义一次，表头和数据行都从它派生。
# 上一版把表头和数据行分成两处手写，改了一处忘了另一处，跑出来表头 15 列、数据 16 列，
# 整张表错位 —— 而列错位肉眼扫过去不一定看得出来，读的人会照着一列错位的数字下结论。
# 所以结构上只留一个定义源，并由 tests/test_eval_depth.py 钉住「表头长度 == 数据行长度」。


def _sig(run: dict, key: str):
    """读信号字段；读不到返回空串。

    历史语料（eval_out/ 里早期那几组）用的是旧版 9 字段 schema，容忍缺字段
    才能拿同一套排版回头渲染它们。缺字段返回空串而不是抛异常，也让
    「表头有的列，数据行一定有值」这条不变量可以被测试直接断言。
    """
    return (run.get("signal") or {}).get(key, "")


TABLE_COLUMNS = [
    ("题目", lambda r: str(r.get("tag") or "")),
    ("耗时", lambda r: f"{r.get('total_s', '')}s"),
    ("检索", lambda r: f"{r.get('retrieve_s', '')}s"),
    ("生成", lambda r: f"{r.get('generate_s', '')}s"),
    ("检索词", lambda r: f"{len(r.get('queries') or [])} 条"),
    ("改写", lambda r: str(r.get("rewrite") or "-")),
    ("命中", lambda r: str(_sig(r, "命中片段"))),
    ("章节", lambda r: str(_sig(r, "命中章节数"))),
    ("带表头", lambda r: str(_sig(r, "带表头片段"))),
    ("年份", lambda r: str(_sig(r, "回答年份数"))),
    ("同比", lambda r: "是" if _sig(r, "含同比") else "否"),
    ("金额", lambda r: str(_sig(r, "金额个数"))),
    ("有据可依率", lambda r: str(_sig(r, "有据可依率"))),
    ("无出处", lambda r: str(_sig(r, "无出处"))),
    ("引用命中率", lambda r: str(_sig(r, "引用命中率"))),
    ("tok", lambda r: str(r.get("completion_tokens"))),
]


def table_header() -> list[str]:
    return [name for name, _ in TABLE_COLUMNS]


def row_for(run: dict) -> list[str]:
    return [render(run) for _, render in TABLE_COLUMNS]


# 控制台摘要要读的信号字段。
# 和表格列一样，这里是「生产者 signal() ↔ 消费者」这个接口唯一被声明的地方。
# `答未找到` 那次事故就是这里断了：signal() 改成产出 `弃答`，摘要还在读旧名字，
# 于是第一道题就 KeyError。所以摘要也走纯函数，并由测试对着 signal() 的输出断言。
SUMMARY_SIGNAL_KEYS = ("命中片段", "命中章节数", "带表头片段", "回答年份数", "含同比", "弃答")


def summary_line(run: dict) -> str:
    """跑完一道题打在屏幕上的那一行。抽成纯函数是为了能被测试直接调用 ——
    原先它内联在 main() 里，测试碰不到，所以崩了也没人知道。"""
    return (f"    {run.get('total_s')}s（检索 {run.get('retrieve_s')} / 生成 {run.get('generate_s')}"
            f" / 扩展 {run.get('analyze_s', 0)}）"
            f"  命中 {_sig(run, '命中片段')} 片 / {_sig(run, '命中章节数')} 节"
            f"  带表头 {_sig(run, '带表头片段')}"
            f"  年份 {_sig(run, '回答年份数')}"
            f"  同比 {'有' if _sig(run, '含同比') else '无'}"
            f"  弃答 {'是' if _sig(run, '弃答') else '否'}"
            f"  扩展 {run.get('analyze_status') or '-'}({_sig(run, '扩展字数')}字)")


def _table(columns, runs: list[dict]) -> str:
    head = "| " + " | ".join(name for name, _ in columns) + " |"
    sep = "|" + "---|" * len(columns)
    body = ["| " + " | ".join(render(r) for _, render in columns) + " |" for r in runs]
    return "\n".join([head, sep] + body)


def markdown_table(runs: list[dict]) -> str:
    return _table(TABLE_COLUMNS, runs)


def _anchors_cell(run: dict) -> str:
    items = _sig(run, "锚点") or []
    return f"{sum(1 for a in items if a.get('中'))}/{len(items)}" if items else "-"


# 细则层单独一张表。和台账分开，是因为两者的读法根本不同：
# 台账读**趋势**（耗时涨了没、章节多样性生效没），细则读**异常**（有没有冒出来的红旗）。
# 混进同一张表，读者会下意识把它们当成同一个分数体系里的列 —— 而它们不是一个体系：
# 台账里几列只是流水账，细则里每一列背后都有一条实测校准过的误报边界。
RUBRIC_COLUMNS = [
    ("题目", lambda r: str(r.get("tag") or "")),
    ("有据可依率", lambda r: str(_sig(r, "有据可依率"))),
    ("无出处", lambda r: str(_sig(r, "无出处"))),
    ("越界引用", lambda r: str(_sig(r, "越界数"))),
    ("换算一致", lambda r: f"{_sig(r, '换算一致')}/{_sig(r, '配对数')}"),
    ("换算不一致", lambda r: str(len(_sig(r, "换算不一致") or []))),
    ("自算不一致", lambda r: f"{_sig(r, '自算不一致数')}/{_sig(r, '自算率检查数')}"),
    ("无引用条目", lambda r: str(_sig(r, "无引用条目数"))),
    ("无据归因", lambda r: str(_sig(r, "无据归因数"))),
    ("锚点", _anchors_cell),
    ("口径缺注", lambda r: "有" if _sig(r, "口径缺注") else "-"),
    ("推断式归因", lambda r: str(_sig(r, "推断式归因数"))),
    ("扩展", lambda r: ("-" if not _sig(r, "扩展状态")
                        else f"{_sig(r, '扩展状态')} {_sig(r, '扩展字数')}字")),
    ("补有据可依率", lambda r: str(_sig(r, "扩展有据可依率"))),
    ("补无据归因", lambda r: str(_sig(r, "扩展无据归因数"))),
]


def rubric_table(runs: list[dict]) -> str:
    return _table(RUBRIC_COLUMNS, runs)


def hit_sections(run: dict) -> list[str]:
    """这道题命中了哪些章节。

    现场跑时 hits 在 run 里；从 json 离线重渲染时 hits 不在、只有 hit_sections。
    两条路都要能走，否则同一份语料没法用两种方式读。
    """
    sections = run.get("hit_sections")
    if sections is None:
        sections = [h.get("section", "") for h in (run.get("hits") or [])]
    return sorted({s for s in sections if s})


def render_report(runs: list[dict], meta: dict, label: str, stamp: str) -> str:
    """整份 markdown 报告的组装。抽成纯函数，就能不花任何 API 调用验证输出路径 ——
    上一版的输出路径从来没有被执行过，所以带着 KeyError 就交付了。"""
    body = [f"# 深度测量 · {label} · {stamp}\n",
            f"- 年报：{meta.get('name')}（{meta.get('pages')} 页 / {meta.get('chunks')} 片段）",
            f"- 配置：`TOP_K={config.TOP_K}` `MAX_PER_SECTION={config.MAX_PER_SECTION}` "
            f"`LLM={config.LLM_MODEL}` `LLM_REWRITE={config.LLM_REWRITE}` "
            f"`ANALYZE={config.ANALYZE_ENABLED}` `WHY_INLINE={config.ANSWER_WHY_INLINE}`\n",
            "## 数字\n", markdown_table(runs), "\n",
            "## 细则\n", rubric_table(runs), "\n", "## 回答\n"]
    for r in runs:
        body.append(f"### [{r.get('tag', '')}] {r.get('question', '')}\n")
        body.append(f"- 耗时 {r.get('total_s')}s（检索 {r.get('retrieve_s')}s / 生成 "
                    f"{r.get('generate_s')}s / 扩展 {r.get('analyze_s')}s）")
        body.append(f"- 检索词：{' ｜ '.join(r.get('queries') or [])}")
        body.append(f"- 命中：{', '.join(hit_sections(r))}\n")
        body.append((r.get("answer") or "") + "\n")
        if r.get("extension"):
            body.append(f"#### 扩展分析（{r.get('analyze_status')}）\n")
            body.append(r["extension"] + "\n")
        elif r.get("analyze_status"):
            body.append(f"#### 扩展分析：{r.get('analyze_status')}\n")
    return "\n".join(body)


def ask(retriever, question, client, history=None, tag=""):
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

    # 第二次调用：解释「为什么」。弃答时不做 —— 没有结论可解释，
    # 让模型去解释一个它根本没找到的数据，它只能编。
    a_diag: dict = {}
    a_usage: dict = {}
    extension = ""
    a_status = "skipped"
    if should_analyze(answer):
        try:
            extension = analyze(question, hits, answer, client=client,
                                company="精达股份", year="2025",
                                usage_out=a_usage, diag=a_diag)
            a_status = a_diag.get("analyze", "ok")
        except Exception as e:
            a_status = "error"
            a_diag["analyze_error"] = str(e)
    t3 = time.perf_counter()

    return {
        "question": question,
        "queries": queries,
        "hits": hits,
        "answer": answer,
        "extension": extension,
        "analyze_status": a_status,
        "analyze_error": a_diag.get("analyze_error"),
        "retrieve_s": round(t1 - t0, 2),
        "generate_s": round(t2 - t1, 2),
        "analyze_s": round(t3 - t2, 2),
        "total_s": round(t3 - t0, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "analyze_prompt_tokens": a_usage.get("prompt_tokens"),
        "analyze_completion_tokens": a_usage.get("completion_tokens"),
        "rewrite": diag.get("rewrite"),
        "rewrite_error": diag.get("rewrite_error"),
        "signal": signal(answer, hits, tag, extension, a_status),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="current")
    ap.add_argument("--no-llm", action="store_true", help="不使用大模型做查询改写")
    ap.add_argument("--no-analysis", action="store_true", help="不做扩展分析（A/B 用）")
    ap.add_argument("--why-inline", action="store_true",
                    help="A/B：把「解释为什么」并进首轮提示词，不调第二次")
    ap.add_argument("--doc", default=DEMO_ID)
    args = ap.parse_args()

    if args.no_llm:
        config.LLM_REWRITE = False
    if args.no_analysis:
        config.ANALYZE_ENABLED = False
    if args.why_inline:
        # 证伪用的对照臂：如果一句话并进首轮就能拿到大部分收益，第二次调用就不划算。
        config.ANSWER_WHY_INLINE = True
        config.ANALYZE_ENABLED = False

    meta, chunks, emb = load_index(args.doc)
    retriever = Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))
    client = get_client()
    print(f"年报：{meta.get('name')}（{meta.get('pages')} 页 / {len(chunks)} 片段）")
    print(f"配置：TOP_K={config.TOP_K} MAX_PER_SECTION={config.MAX_PER_SECTION} "
          f"LLM={config.LLM_MODEL} LLM_REWRITE={config.LLM_REWRITE} "
          f"ANALYZE={config.ANALYZE_ENABLED} WHY_INLINE={config.ANSWER_WHY_INLINE}\n")

    runs = []
    for tag, q in QUESTIONS:
        print(f"[{tag}] {q}")
        r = ask(retriever, q, client, tag=tag)
        r["tag"] = tag
        runs.append(r)
        print(summary_line(r) + "\n")

    print(f"[追问] {FOLLOWUP[0]}  →  {FOLLOWUP[1]}")
    r1 = ask(retriever, FOLLOWUP[0], client, tag="追问-1")
    r2 = ask(retriever, FOLLOWUP[1], client, history=[{"q": FOLLOWUP[0]}], tag="追问-2")
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

    table = markdown_table(runs) + "\n\n" + rubric_table(runs)

    (OUT_DIR / f"{stamp}.md").write_text(
        render_report(runs, meta, args.label, stamp), encoding="utf-8")
    # 片段正文必须一起落盘。上一版只存了 hit_sections / hit_subsection，把 hits 正文丢掉，
    # 后果是所有依赖片段的检查（有据可依率、越界引用、引文核对、口径标注）都无法离线复算 ——
    # 每加一条新检查就得重新花真实 API 调用跑一遍。存下来之后，历史语料可以随时被新检查回算。
    (OUT_DIR / f"{stamp}.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "hits"} |
                    {"hit_sections": [h.get("section") for h in r["hits"]],
                     "hit_subsection": [h.get("subsection") for h in r["hits"]],
                     "hit_texts": [h.get("text") or "" for h in r["hits"]]}
                    for r in runs], ensure_ascii=False, indent=2), encoding="utf-8")

    print(table)
    md = OUT_DIR / f"{stamp}.md"
    print(f"\n回答全文写入 {md.relative_to(ROOT)}（同目录另有 json）")


if __name__ == "__main__":
    main()
