"""上线评测：合并前跑一套固定的评测，结果追加到 docs/eval/history.jsonl，再重新生成评测页的快照。

用法：python -m tools.release_eval [--note 这次改了什么] [--skip-model]
     --skip-model 只跑不调模型的几项（单元测试、三表、体检），用来快速自查，不写上线记录。

什么时候跑（用户 2026-11-03 定）：改了 rag/ 或 ui/ 才跑全套；只改文档或 web/ 时跳过。
全套约十几分钟，会花一点 API 费用。每一项都是调用现成的评测脚本，这里只负责按固定参数跑、收结果。

固定参数：端到端用 plain 引擎（线上默认的通俗版）、并发 3（并发 5 会连接报错）；问题理解用 planner 引擎。
结果文件都带同一个 rel-<时间> 标签，评测页的例子（真实回退、注入的错误）从这些文件里取。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval_out"
HISTORY = ROOT / "docs" / "eval" / "history.jsonl"


def _run(args: list[str]) -> str:
    """跑一个评测脚本，返回它的标准输出；失败直接抛出 —— 上线评测有一项没跑成就不能算数。"""
    print("·", " ".join(args), flush=True)
    done = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 失败：\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return done.stdout


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def tests() -> dict:
    out = _run(["-m", "pytest", "-q"])
    m = re.search(r"(\d+) passed", out)
    failed = re.search(r"(\d+) failed", out)
    return {"passed": int(m.group(1)) if m else 0, "failed": int(failed.group(1)) if failed else 0}


def statements() -> dict:
    _run(["-m", "tools.eval_statements"])
    d = json.loads((OUT / "statements.json").read_text(encoding="utf-8"))
    return {k: d[k] for k in ("reports", "tables_ok", "tables_n", "cross_ok", "cross_n", "ans_ok", "ans_n")}


def healthcheck() -> dict:
    _run(["-m", "tools.eval_healthcheck"])
    d = json.loads((OUT / "healthcheck.json").read_text(encoding="utf-8"))
    return {k: d[k] for k in ("ok", "n", "reports")}


def intent(tag: str) -> dict:
    _run(["-m", "tools.eval_intent", "--engine", "planner", "--tag", tag])
    rows = json.loads((OUT / f"intent-planner-{tag}.json").read_text(encoding="utf-8"))
    return {"ok": sum(1 for r in rows if r["score"].get("ok")), "n": len(rows), "tag": f"intent-planner-{tag}"}


def e2e(tag: str) -> dict:
    _run(["-m", "tools.eval_e2e", "--engines", "plain", "--workers", "3", "--tag", tag])
    rows = json.loads((OUT / f"e2e-{tag}.json").read_text(encoding="utf-8"))
    secs = [r["secs"] for r in rows if isinstance(r.get("secs"), (int, float)) and r["secs"] > 5]
    return {"ok": sum(1 for r in rows if r["ok"]), "n": len(rows),
            "orphan": sum(r.get("orphan") or 0 for r in rows),
            "retried": sum(1 for r in rows if r.get("retries")),
            "secs_median": round(median(secs), 1) if secs else None, "tag": f"e2e-{tag}"}


def review(tag: str) -> dict:
    _run(["-m", "tools.eval_review", "--tag", tag])
    rows = json.loads((OUT / f"review-{tag}.json").read_text(encoding="utf-8"))
    good = [r for r in rows if r["expect"] == "pass"]
    bad = [r for r in rows if r["expect"] == "block"]
    return {"pass_ok": sum(r["ok"] for r in good), "pass_n": len(good),
            "block_ok": sum(r["ok"] for r in bad), "block_n": len(bad), "tag": f"review-{tag}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", default="", help="这次上线改了什么（写进上线记录）")
    ap.add_argument("--skip-model", action="store_true", help="只跑不调模型的几项，不写上线记录")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    now = datetime.now()
    tag = f"rel-{now:%m%d-%H%M}"
    record = {"date": f"{now:%Y-%m-%d %H:%M}", "commit": _git("rev-parse", "--short", "HEAD"),
              "dirty": bool(_git("status", "--porcelain", "--", "rag", "ui", "app.py", "tools")),
              "note": args.note, "tests": tests(), "statements": statements(), "healthcheck": healthcheck()}
    if args.skip_model:
        print(json.dumps(record, ensure_ascii=False, indent=1))
        return
    from rag import config
    record.update(model=config.LLM_MODEL, intent=intent(tag), e2e=e2e(tag), review=review(tag))
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=1))

    from tools.build_eval_snapshot import main as build
    build()


if __name__ == "__main__":
    main()
