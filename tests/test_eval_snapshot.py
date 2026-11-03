"""评测页快照（tools/build_eval_snapshot.py）：数据从哪来、例子怎么挑、缺文件时要报错。"""
import json

import pytest

from tools import build_eval_snapshot as snap

E2E_MD = """# 端到端对比 · 2026-11-03 21:21

| 指标 | plain | nostmt |
|---|---|---|
| 整题合格 | 7 / 7 | 5 / 7 |
| 关键数字都在 | 7 / 7 | 6 / 7 |
| 无出处数字（合计） | 0 | 2 |

## 逐题

| # | 问题 | plain | nostmt |
"""

RECORD = {
    "date": "2026-11-03 23:00", "commit": "b0b347d", "model": "deepseek-flash", "note": "首次",
    "tests": {"passed": 780, "failed": 0},
    "statements": {"reports": 21, "tables_ok": 63, "tables_n": 63, "cross_ok": 98, "cross_n": 98, "ans_ok": 20, "ans_n": 20},
    "healthcheck": {"ok": 96, "n": 96, "reports": 15},
    "intent": {"ok": 59, "n": 60, "tag": "intent-planner-rel-x"},
    "e2e": {"ok": 37, "n": 37, "orphan": 1, "retried": 1, "secs_median": 24.0, "tag": "e2e-rel-x"},
    "review": {"pass_ok": 8, "pass_n": 8, "block_ok": 33, "block_n": 33, "tag": "review-rel-x"},
}

GOOD = "精达股份 2025 年营业收入为 246.74 亿元[4]。归母净利润 5.99 亿元[4]。\n\n**这意味着什么**\n- 收入增长。"


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "OUT", tmp_path)
    monkeypatch.setattr(snap, "ROOT", tmp_path)
    monkeypatch.setattr(snap, "HISTORY", tmp_path / "history.jsonl")
    (tmp_path / "history.jsonl").write_text(json.dumps(RECORD, ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "e2e-rel-x.json").write_text(json.dumps([
        {"q": "有息负债有多少？", "ok": True, "answer": GOOD,
         "retries": [{"action": "retrieve", "note": "漏了长期借款", "report": "", "first": "年报中未找到有息负债。"}]},
        {"q": "研发投入多少？", "ok": True, "answer": GOOD, "retries": []},
        {"q": "回退了但没答对", "ok": False, "answer": GOOD,
         "retries": [{"action": "regenerate", "note": "x", "report": "", "first": "y"}]},
    ], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "review-rel-x.json").write_text(json.dumps([
        {"q": "营收多少", "kind": "好回答", "expect": "pass", "ok": True, "verdict": "pass", "note": "",
         "answer": GOOD, "text": GOOD},
        {"q": "营收多少", "kind": "改数字", "expect": "block", "ok": True, "verdict": "regenerate",
         "note": "286.74 与表格 246.74 矛盾", "answer": GOOD, "text": GOOD.replace("246.74", "286.74")},
        {"q": "营收多少", "kind": "改数字", "expect": "block", "ok": True, "verdict": "regenerate",
         "note": "第二例不要", "answer": GOOD, "text": GOOD.replace("5.99", "45.99")},
        {"q": "营收多少", "kind": "编原因", "expect": "block", "ok": False, "verdict": "pass",
         "note": "没拦下的不展示", "answer": GOOD, "text": GOOD + "主要因为提价 15%。"},
    ], ensure_ascii=False), encoding="utf-8")
    for stem in ("e2e-stmt-ab", "e2e-style-r2", "e2e-multi-b"):
        md = E2E_MD
        if stem == "e2e-style-r2":
            md = md.replace("plain | nostmt", "new | plain")
        if stem == "e2e-multi-b":
            md = md.replace("plain | nostmt", "plain | flat")
        (tmp_path / f"{stem}.md").write_text(md, encoding="utf-8")
    return tmp_path


def test_md_metrics(out):
    head, rows = snap._md_metrics("e2e-stmt-ab")
    assert head == ["plain", "nostmt"]
    assert rows["整题合格"] == ["7 / 7", "5 / 7"] and "#" not in rows      # 只读开头那张汇总表


def test_lead_strips_citations_and_stops_at_heading():
    s = snap._lead(GOOD)
    assert s == "精达股份 2025 年营业收入为 246.74 亿元。归母净利润 5.99 亿元。"
    table = "结论[1]。\n\n| 项目 | 2025 年 |\n|---|---|\n| 营业收入 | 246.74 亿元 [4] |"
    assert snap._lead(table).endswith("营业收入：246.74 亿元")
    assert snap._lead("长" * 400).endswith("……")


def test_caught_only_successful_retries(out):
    got = snap.caught_examples("e2e-rel-x")
    assert [x["q"] for x in got] == ["有息负债有多少？"]
    assert got[0]["note"] == "漏了长期借款" and got[0]["first"] == "年报中未找到有息负债。"
    assert got[0]["final"].startswith("精达股份 2025 年营业收入")


def test_injected_one_per_kind_with_diff(out):
    got = snap.injected_examples("review-rel-x")
    assert [x["kind"] for x in got] == ["改数字"]                       # 没拦下的、重复的种类都不要
    assert "246.74" in got[0]["before"] and "286.74" in got[0]["after"]


def test_build_full_snapshot(out):
    s = snap.build()
    assert s["release"] == {"date": "2026-11-03 23:00", "commit": "b0b347d", "model": "deepseek-flash", "note": "首次"}
    assert [h["value"] for h in s["headline"]] == ["37/37", "1", "33/33", "63/63"]
    assert [x["name"] for x in s["suites"]][:3] == ["端到端", "问题理解", "复核器"]
    assert s["experiments"][0]["cols"] == ["接三表片段", "不接"]
    assert s["experiments"][0]["rows"][0] == ["整题合格", "7 / 7", "5 / 7"]
    assert s["history"][0]["e2e"] == "37/37" and s["history"][0]["review"] == "8/8 · 33/33"
    assert all(len(m["commit"]) == 7 for m in s["milestones"])


def test_missing_file_is_an_error(out):
    (out / "e2e-multi-b.md").unlink()
    with pytest.raises(FileNotFoundError):
        snap.build()


def test_page_reads_snapshot_and_survives_missing(tmp_path, monkeypatch):
    from ui import eval_component

    monkeypatch.setattr(eval_component, "SNAPSHOT", tmp_path / "nope.json")
    assert eval_component.load_snapshot() == {}
    (tmp_path / "s.json").write_text('{"headline": []}', encoding="utf-8")
    monkeypatch.setattr(eval_component, "SNAPSHOT", tmp_path / "s.json")
    assert eval_component.load_snapshot() == {"headline": []}


def test_review_misses_listed_with_annotation(out, monkeypatch):
    monkeypatch.setitem(snap.ANNOTATIONS, ("review-rel-x", "编原因", "营收多少"), "人工核对说明")
    got = snap.review_misses("review-rel-x")
    assert got == [{"kind": "编原因", "q": "营收多少", "verdict": "pass", "note": "没拦下的不展示",
                    "comment": "人工核对说明"}]
    assert snap.build()["misses"] == got
