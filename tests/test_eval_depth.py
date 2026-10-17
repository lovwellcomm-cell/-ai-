"""测量脚本自身可测性的测试。

这里的每一条都对应一次真实事故。共同点是：**bug 只存在于 main() 里**，
纯函数测试（test_eval_fidelity.py 那一套）永远碰不到，所以 76 个测试全绿也没拦住。

  · `signal()` 不再产出 `答未找到`，而 main() 的打印还在读它 → 第一道题 KeyError，
    脚本在写任何文件之前就崩掉。
  · 表头手写了 15 列、数据行手写了 16 列 → 表格整体错位，读的人照着一列错位的数字下结论。

所以这一轮把「排版」从 main() 里抽出来变成 row_for() / markdown_table() 两个纯函数，
让上面两类错误可以被断言钉死。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eval_depth import (  # noqa: E402
    RUBRIC_COLUMNS, SUMMARY_SIGNAL_KEYS, TABLE_COLUMNS, extension_signals, markdown_table,
    row_for, rubric_table, signal, summary_line, table_header,
)


def fake_run(**over):
    """一个结构完整的 run 对象：signal 用的是 signal() 的真实输出，不是手写的 dict。

    这点很关键 —— 手写 signal 字典的测试无法发现「生产者和消费者字段名对不上」，
    而那正是这次的 bug。
    """
    answer = over.pop("answer", "2025年营业收入为 246.74亿元（24,673,670,269.16元）[1]。")
    run = {
        "tag": "简单",
        "question": "2025年营业收入是多少？",
        "queries": ["2025年营业收入"],
        "answer": answer,
        "retrieve_s": 1.2,
        "generate_s": 9.5,
        "analyze_s": 0.0,
        "total_s": 10.7,
        "prompt_tokens": 3000,
        "completion_tokens": 500,
        "extension": "",
        "analyze_status": "skipped",
        "rewrite": "ok",
        "rewrite_error": None,
        "signal": signal(answer, []),
    }
    run.update(over)
    return run


def test_header_and_row_have_the_same_width():
    """列错位事故的回归测试。"""
    header, row = table_header(), row_for(fake_run())
    assert len(header) == len(row) == len(TABLE_COLUMNS)


def test_every_column_renders_something():
    """字段改名事故（`答未找到`）的回归测试。

    _sig() 读不到字段时返回空串，所以「某一格是空的」就等价于
    「这一列读的字段，signal() 已经不产出了」。这条断言把那种错误变成红灯，
    而不是等到跑真测量时才 KeyError。
    """
    header, row = table_header(), row_for(fake_run())
    empty = [name for name, cell in zip(header, row) if cell == ""]
    assert not empty, f"这些列读不到字段（signal() 里可能已经改名或删除）：{empty}"


def test_signal_still_produces_every_field_the_display_layer_reads():
    """对着 signal() 的输出键断言，直接指出是哪个字段没了。

    这一条才是 `答未找到` 事故的真正回归测试：那个字段只被控制台摘要读，
    不在任何表格列里，所以光测 row_for() 是抓不到的。
    """
    produced = set(signal("", []))
    assert set(SUMMARY_SIGNAL_KEYS) <= produced, \
        f"signal() 缺少控制台摘要要读的字段：{set(SUMMARY_SIGNAL_KEYS) - produced}"
    # 表格列读的字段名散在各列的 lambda 里，这里显式列一遍
    needed = {"命中片段", "命中章节数", "带表头片段", "回答年份数", "含同比",
              "金额个数", "有据可依率", "无出处", "引用命中率"}
    assert needed <= produced, f"signal() 缺少：{needed - produced}"


def test_summary_line_renders_every_field():
    """摘要那一行也必须被纯函数测试覆盖 —— 它曾经内联在 main() 里，
    所以 KeyError 要等到真跑一次测量才暴露，而那要花真实 API 调用。"""
    line = summary_line(fake_run())
    assert "命中 0 片 / 0 节" in line
    assert "弃答 否" in line
    assert "None" not in line, f"有字段没渲染出来：{line}"

    # 布尔字段必须用一个该显示「是」的样本来测：字段若被改名，_sig 返回空串 →
    # 渲染成「否」，和真实的 False 长得一模一样，只有这一半能把它抓出来。
    abstaining = fake_run(answer="年报中未找到相关信息。")
    assert "弃答 是" in summary_line(abstaining)


def test_markdown_table_is_well_formed():
    """表头行、分隔行、数据行的竖线数量必须一致，否则 markdown 渲染出来的表是错的。"""
    table = markdown_table([fake_run(), fake_run(tag="趋势")])
    lines = table.splitlines()
    assert len(lines) == 2 + 2
    widths = {ln.count("|") for ln in lines}
    assert len(widths) == 1, f"各行竖线数量不一致：{widths}"


def test_rubric_table_is_well_formed_too():
    """细则表同样不能被写歪。两张表各有各的列定义，都得钉住。"""
    runs = [fake_run(), fake_run(tag="趋势")]
    table = rubric_table(runs)
    lines = table.splitlines()
    assert len(lines) == 2 + len(runs)
    assert len({ln.count("|") for ln in lines}) == 1, table


def test_rubric_columns_read_fields_signal_actually_produces():
    """同上，但针对细则表。`锚点` 这类字段来自 rubric_questions，字段名对不上会渲染成空。"""
    row = [render(fake_run()) for _, render in RUBRIC_COLUMNS]
    empty = [name for (name, _), cell in zip(RUBRIC_COLUMNS, row) if cell == ""]
    assert not empty, f"细则表这些列读不到字段：{empty}"


def test_extension_arithmetic_is_measured():
    """扩展段自己的算术也要查。

    早先只量了扩展段的保真度和归因，于是 `X亿元（Y元）` 配错、比率算错，一条信号都不响 ——
    而这段正是新代码，也是最可能出错的地方。
    """
    bad = extension_signals("差额18.53亿元（999,999,999.99元）[1]。", [{"text": "x"}], "ok")
    assert bad["扩展换算不一致"] and bad["扩展红旗数"] >= 1

    good = extension_signals("差额18.53亿元（1,852,557,535.42元）[1]。", [{"text": "x"}], "ok")
    assert good["扩展红旗数"] == 0, good


def test_no_extension_reports_zero_flags_not_missing_keys():
    """没有扩展时键也要在，否则表格会出现空格、`_sig` 会静默返回空串。"""
    r = extension_signals("", [{"text": "x"}], "no_add")
    assert r["扩展红旗数"] == 0 and r["扩展状态"] == "no_add"
    assert r["扩展换算不一致"] == []


def test_row_for_tolerates_legacy_runs_without_signal_fields():
    """历史语料是旧版 9 字段 schema，缺字段要渲染成空串而不是抛异常 ——
    否则没法用同一套排版回头对比 2026-10-16 那 5 组存档。"""
    legacy = {"tag": "简单", "total_s": 12.0, "queries": [], "signal": {"命中片段": 8}}
    row = row_for(legacy)
    assert len(row) == len(TABLE_COLUMNS)
    assert row[table_header().index("命中")] == "8"
    assert row[table_header().index("有据可依率")] == ""    # 旧存档没这个字段


def test_ask_keeps_hits_for_persistence(monkeypatch):
    """hits 必须留在 run 里 —— 落盘时要用它写 hit_texts。

    上一版把它丢掉，导致所有依赖片段的检查都无法离线复算，每加一条新检查
    就要重跑一次真实 API 调用。这条断言防止有人再把它剔掉。
    """
    from tools import eval_depth

    hit = {"text": "营业收入 24,673,670,269.16", "section": "第八节",
           "subsection": "合并利润表", "page_start": 1, "page_end": 1}

    class FakeRetriever:
        def search_with_queries(self, q, **kw):
            return [hit], ["2025年营业收入"]

    monkeypatch.setattr(eval_depth, "generate", lambda *a, **k: iter(["回答内容"]))
    run = eval_depth.ask(FakeRetriever(), "2025年营业收入是多少？", client=object())
    assert run["hits"] == [hit]
    # 并且 signal 是真的基于这些片段算出来的，不是占位值
    assert run["signal"]["命中片段"] == 1
