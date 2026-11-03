"""评测页的数据：从上线记录和 eval_out/ 里挑出要展示的，生成 docs/eval/snapshot.json（入库）。

用法：python -m tools.build_eval_snapshot　（tools/release_eval.py 跑完会自动调用）

线上从不跑评测（要花钱、要 Key，eval_out/ 也不入库），评测页只展示这份快照。
数据来源都是白名单：
  · docs/eval/history.jsonl 的最后一条 = 最近一次上线评测，总览数字和评测卡片用它；
    它带的结果文件（e2e-rel-…、review-rel-…）里挑例子：复核真实回退过的题、注入错误各一例；
  · EXPERIMENTS：几组「有 vs 没有」的对照实验，从 eval_out/ 里对应的结果表读数；
  · MILESTONES：一路怎么改进的。数字抄自当时的提交说明（GitHub 上可以点开核对），题库一直在变，
    每条写明当时的题数，不能横着比。
白名单里的文件缺了直接报错，不悄悄少一项。
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval_out"
HISTORY = ROOT / "docs" / "eval" / "history.jsonl"
SNAPSHOT = ROOT / "docs" / "eval" / "snapshot.json"
REPO = "https://github.com/lovwellcomm-cell/-ai-"

# 对照实验：(标题, 说明, 结果表文件, [(列名, 显示名)], 要展示的指标, 补充说明)
EXPERIMENTS = [
    ("三表数据进回答", "问到有息负债、负债率、自由现金流这类报表科目时，把勾稽核对过的三张表做成片段接给模型。同一批题，接与不接各跑一遍。",
     "e2e-stmt-ab", [("plain", "接三表片段"), ("nostmt", "不接")],
     ["整题合格", "关键数字都在", "误答未找到", "无出处数字（合计）", "耗时中位数（秒）"],
     "7 道报表科目题。原有 30 题一起跑时：接 30/30，不接 28/30。"),
    ("通俗版回答", "同样的准确性约束，回答改成「结论 → 关键数字表 → 这意味着什么 → 需要留意」的版式。",
     "e2e-style-r2", [("new", "专业版（原提示词）"), ("plain", "通俗版")],
     ["整题合格", "关键数字都在", "无出处数字（合计）", "首句带数字"],
     "15 题。「无出处数字」是回答里在片段中找不到出处的数字个数，越少越好。"),
    ("按要点分别检索", "一问多项（研发人员、供应商占比、员工总数、分红……分散在不同章节）时，每个要点先各取几段，再合起来回答。",
     "e2e-multi-b", [("plain", "按要点检索"), ("flat", "一次融合排名")],
     ["整题合格", "关键数字都在"],
     "8 道一问多项题。"),
]

# 里程碑：(日期, 提交, 做了什么, 结果, 说明)
MILESTONES = [
    ("2026-11-01", "d14b21c", "回答里的数字整理成表格，逐格核对出处", "",
     "找不到出处的数字在界面上用虚线标出来。之后所有评测都统计「无出处数字」。"),
    ("2026-11-02", "2ecbae0", "建「理解问题」测试集", "旧流程 37/48",
     "48 题：问多年时停在季报上、超范围的问题照样当报告题答、模糊问题不反问。"),
    ("2026-11-02", "170b537", "规划器：一次调用理解意图、目标报告、要点和检索词", "48/48",
     "同一套 48 题。"),
    ("2026-11-02", "ad54ac8", "复核器：回答交出去之前查要点、依据和漏看，不过关最多回退一次", "放行 6/6，拦下 23/23",
     "6 个正常回答，23 处人为注入的错误。"),
    ("2026-11-02", "74a93ed", "端到端评测：新旧流程对比", "新流程 15/15、15/15；旧流程 9/15、10/15",
     "15 题，各跑两轮。据此修了三处：复核放行了「未找到」、模型偶尔返回空回答、回退时间预算。"),
    ("2026-11-03", "986e379", "跨报告回答：先读另一份报告记笔记，再在主报告上回答", "端到端 19/20、20/20；跨报告 5/5",
     "端到端扩到 20 题，问题理解扩到 60 题（60/60、59/60）。"),
    ("2026-11-03", "5da15e9", "跨口径检查：九个月比十二个月这类计算由程序查出、直接判重写", "复核器拦下跨口径 2/2",
     "复核器评测加了「跨口径」注入。"),
    ("2026-11-03", "152cd61", "三表速读：三张合并报表程序抽取，勾稽对不上的表不出", "出表 63/63，交叉核对 98/98",
     "本机 21 份报告（含季报、半年报），不调模型。"),
    ("2026-11-03", "9e389be", "三表数据进回答", "新题 7/7（不接 5/7）；原 30 题 30/30（不接 28/30）",
     "端到端扩到 37 题。"),
]

# 人工核对过的判错例子：(上线评测的复核结果文件, 类型, 题目) → 说明。只对那一次评测的那一题生效
ANNOTATIONS = {
    ("review-rel-1103-2254", "好回答", "七匹狼 2025 年报和 2026 三季报的营收、净利润对比"):
        "人工核对：这份「正常回答」是模型生成的，这次它自己算错了 —— 20.26 − 20.25 亿元应为约 0.01 亿元，"
        "回答写成 0.11 亿元。复核器拦下是对的，评测按规则仍记为判错。",
    ("review-rel-1103-2254", "改数字", "七匹狼 2025 年报和 2026 三季报的营收、净利润对比"):
        "复核调用本身出错（没有返回判断），不是放行。线上遇到这种情况时回答照常给出、不标「已复核」。",
    ("review-rel-1103-2307", "答非所问", "营业收入和归母净利润分别是多少？"):
        "复核调用本身出错（没有返回判断），不是放行。线上遇到这种情况时回答照常给出、不标「已复核」。",
}

LIMITS = [
    "题目是作者自建的，覆盖精达股份、时代新材、贵州茅台、七匹狼、万科等约 15 家公司的报告，不是公开基准。",
    "用真实模型跑，同一套题两次结果会有一两题的波动；比较改动时和对照组一起跑。",
    "复核和逐格核对查的是「回答有没有出处」，报告本身写错了、或者模型把原文读反了，查不出来。",
    "「注入的错误」是人为改出来的压力测试，真实回答里的错误往往更隐蔽。",
    "银行、证券、保险公司的报表结构不同，三表评测不覆盖。",
]


def _need(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"评测页要用的文件不在：{path.relative_to(ROOT)}（先在本机跑对应的评测）")
    return path


def _md_metrics(stem: str) -> tuple[list[str], dict[str, list[str]]]:
    """eval_e2e 结果表 → (引擎列名, {指标: [各列的值]})。只读开头那张「| 指标 | … |」汇总表。"""
    lines = _need(OUT / f"{stem}.md").read_text(encoding="utf-8").splitlines()
    head, rows = None, {}
    for ln in lines:
        if ln.startswith("| 指标 |"):
            head = [c.strip() for c in ln.strip("|").split("|")][1:]
            continue
        if head is not None:
            if not ln.startswith("|"):
                break
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if cells[0] and not set(cells[0]) <= set("-"):
                rows[cells[0]] = cells[1:]
    if head is None:
        raise ValueError(f"{stem}.md 里没有汇总表")
    return head, rows


def _plain(text: str) -> str:
    """回答原文 → 页面上展示的纯文字：去掉引用编号、加粗、表格分隔行。"""
    s = re.sub(r"\[(?:网)?\d+(?:[,，、]\s*\d+)*\]", "", text or "")
    s = s.replace("**", "")
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln and not re.fullmatch(r"\|?[\s:|-]+\|?", ln)).strip()


def _lead(text: str, limit: int = 260) -> str:
    """回答的结论部分：第一个小标题之前，最多 limit 字。表格行换成「项目：数值」的形式，读起来不乱。"""
    body = _plain(text).split("这意味着什么")[0].split("扩展分析")[0]
    out = []
    for ln in body.splitlines():
        if ln.startswith("|"):
            if not re.search(r"\d", ln):          # 表头（「项目 | 本期 | 上期」）不要
                continue
            cells = [c.strip() for c in ln.strip("|").split("|") if c.strip()]
            ln = "：".join(cells[:1]) + ("：" + "，".join(cells[1:]) if len(cells) > 1 else "")
        out.append(ln)
    s = "\n".join(out).strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "……"


def caught_examples(e2e_stem: str, limit: int = 4) -> list[dict]:
    """复核真实回退过、最后合格的题：回退前那一版、复核理由、最终回答。"""
    rows = json.loads(_need(OUT / f"{e2e_stem}.json").read_text(encoding="utf-8"))
    out = []
    for r in rows:
        if not r.get("retries") or not r.get("ok"):
            continue
        x = r["retries"][0]
        out.append({"q": r["q"], "action": x["action"], "report": x.get("report") or "",
                    "note": x["note"], "first": _lead(x["first"]), "final": _lead(r["answer"])})
    # 第一版说「未找到」、回退后答出来的排前面：这是复核最直接的价值
    out.sort(key=lambda x: "未找到" not in x["first"])
    return out[:limit]


def _sents(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？\n])", _plain(text)) if s.strip()]


def injected_examples(review_stem: str) -> list[dict]:
    """注入的错误每种挑一例（复核拦下了的）：原句、改后的句子、复核理由。"""
    rows = json.loads(_need(OUT / f"{review_stem}.json").read_text(encoding="utf-8"))
    out, seen = [], set()
    for r in rows:
        if r["expect"] != "block" or not r["ok"] or r["kind"] in seen:
            continue
        a, b = _sents(r["answer"]), _sents(r["text"])
        before, after = "", ""
        if r["kind"] == "答非所问":
            before, after = "（原本回答这道题的内容）", "（整段换成了另一道题的回答）" + (b[0].strip()[:80] if b else "")
        else:
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
                if op != "equal":
                    # 句子后面常粘着表格（「…5.99 亿元。| 项目 | 2025」）：只留句子本身
                    before = "".join(a[i1:i2]).split("|")[0].strip()[:160]
                    after = "".join(b[j1:j2]).split("|")[0].strip()[:160]
                    break
        seen.add(r["kind"])
        out.append({"kind": r["kind"], "q": r["q"], "verdict": r["verdict"], "note": r["note"],
                    "before": before or "（原回答里没有这句）", "after": after})
    return out


def review_misses(review_stem: str) -> list[dict]:
    """复核器这次判错的用例（正常回答被拦、注入的错误没拦下或调用出错），原样列出，有人工核对的附上说明。"""
    rows = json.loads(_need(OUT / f"{review_stem}.json").read_text(encoding="utf-8"))
    return [{"kind": r["kind"], "q": r["q"], "verdict": r["verdict"], "note": r["note"],
             "comment": ANNOTATIONS.get((review_stem, r["kind"], r["q"]), "")}
            for r in rows if not r["ok"]]


def _dedup(items: list[dict], key: str) -> list[dict]:
    seen, out = set(), []
    for x in items:
        if x[key] not in seen:
            seen.add(x[key])
            out.append(x)
    return out


def experiments() -> list[dict]:
    out = []
    for title, what, stem, cols, metrics, note in EXPERIMENTS:
        head, rows = _md_metrics(stem)
        idx = [head.index(c) for c, _ in cols]
        out.append({"title": title, "what": what, "cols": [n for _, n in cols], "note": note,
                    "rows": [[m] + [rows[m][i] for i in idx] for m in metrics if m in rows]})
    return out


def suites(rec: dict) -> list[dict]:
    """每套评测一张卡。数字全部来自这次上线评测的记录。"""
    e, i, rv, s, h, t = rec["e2e"], rec["intent"], rec["review"], rec["statements"], rec["healthcheck"], rec["tests"]
    return [
        {"name": "端到端", "what": "从提问到回答整条流程：用对报告、期望的关键数字都在、不误答「未找到」、跨报告题引用了笔记。",
         "result": f"{e['ok']} / {e['n']} 合格", "detail": f"回答里找不到出处的数字共 {e['orphan']} 个；"
         f"{e['retried']} 题复核后回退过；耗时中位数 {e['secs_median']} 秒",
         "cmd": "python -m tools.eval_e2e --engines plain --workers 3", "model": True},
        {"name": "问题理解", "what": "规划器：意图、目标报告（哪家公司、哪一年、哪种报告）、要不要出指标卡、跨报告时先读哪份。",
         "result": f"{i['ok']} / {i['n']} 全对", "detail": "每题逐项比对，一项不对就算错",
         "cmd": "python -m tools.eval_intent --engine planner", "model": True},
        {"name": "复核器", "what": "正常回答要放行；往正常回答里注入错误（改数字、编原因、错年份、答非所问、跨口径计算）要拦下。",
         "result": f"放行 {rv['pass_ok']} / {rv['pass_n']}，拦下 {rv['block_ok']} / {rv['block_n']}",
         "detail": "错误是人为注入的压力测试", "cmd": "python -m tools.eval_review", "model": True},
        {"name": "三表抽取", "what": "三张合并报表：勾稽自检通过才出表；和「主要会计数据」表交叉核对；人工核过的难点行。",
         "result": f"出表 {s['tables_ok']} / {s['tables_n']}",
         "detail": f"{s['reports']} 份报告；交叉核对 {s['cross_ok']} / {s['cross_n']}；人工答案 {s['ans_ok']} / {s['ans_n']}",
         "cmd": "python -m tools.eval_statements", "model": False},
        {"name": "体检抽取", "what": "财报体检 9 项的抽取结果，对照人工核过的答案（审计意见、前五名占比、商誉、政府补助……）。",
         "result": f"{h['ok']} / {h['n']}", "detail": f"{h['reports']} 份报告",
         "cmd": "python -m tools.eval_healthcheck", "model": False},
        {"name": "单元测试", "what": "抽取规则、回合状态机、浏览器存档校验、界面数据整形等，夹具多是真实报告的原文行。",
         "result": f"{t['passed']} 个通过" + (f"，{t['failed']} 个失败" if t["failed"] else ""), "detail": "",
         "cmd": "python -m pytest -q", "model": False},
    ]


def headline(rec: dict) -> list[dict]:
    e, rv, s = rec["e2e"], rec["review"], rec["statements"]
    return [
        {"label": "端到端题目合格", "value": f"{e['ok']}/{e['n']}", "sub": "用对报告、关键数字都在、不误答"},
        {"label": "回答里无出处的数字", "value": str(e["orphan"]), "sub": f"{e['n']} 题合计，逐格核对查出"},
        {"label": "复核拦下注入的错误", "value": f"{rv['block_ok']}/{rv['block_n']}",
         "sub": f"正常回答放行 {rv['pass_ok']}/{rv['pass_n']}"},
        {"label": "三张表勾稽通过", "value": f"{s['tables_ok']}/{s['tables_n']}", "sub": f"{s['reports']} 份报告，不调模型"},
    ]


def build() -> dict:
    lines = [ln for ln in _need(HISTORY).read_text(encoding="utf-8").splitlines() if ln.strip()]
    history = [json.loads(ln) for ln in lines]
    rec = history[-1]
    return {
        "generated": f"{datetime.now():%Y-%m-%d %H:%M}",
        "repo": REPO,
        "release": {k: rec[k] for k in ("date", "commit", "model", "note") if k in rec},
        "headline": headline(rec),
        "suites": suites(rec),
        # 真实回退的例子从历次上线评测里收（新的在前，同一道题只留一次）：一次评测里触发回退的题本来就少
        "caught": sorted(_dedup([x for h in reversed(history) for x in caught_examples(h["e2e"]["tag"])], "q"),
                         key=lambda x: "未找到" not in x["first"])[:4],
        "injected": injected_examples(rec["review"]["tag"]),
        "misses": review_misses(rec["review"]["tag"]),
        "experiments": experiments(),
        "milestones": [{"date": d, "commit": c, "title": t, "result": r, "note": n} for d, c, t, r, n in MILESTONES],
        "history": [{"date": h["date"], "commit": h["commit"], "note": h.get("note", ""),
                     "e2e": f"{h['e2e']['ok']}/{h['e2e']['n']}", "orphan": h["e2e"]["orphan"],
                     "intent": f"{h['intent']['ok']}/{h['intent']['n']}",
                     "review": f"{h['review']['pass_ok']}/{h['review']['pass_n']} · {h['review']['block_ok']}/{h['review']['block_n']}",
                     "statements": f"{h['statements']['tables_ok']}/{h['statements']['tables_n']}",
                     "tests": h["tests"]["passed"]} for h in reversed(history)],
        "limits": LIMITS,
    }


def main() -> None:
    snap = build()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"写入 {SNAPSHOT.relative_to(ROOT)}：真实回退 {len(snap['caught'])} 例，注入错误 {len(snap['injected'])} 例，"
          f"对照实验 {len(snap['experiments'])} 组，上线记录 {len(snap['history'])} 条")


if __name__ == "__main__":
    main()
