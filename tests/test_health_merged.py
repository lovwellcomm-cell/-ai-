"""财报体检（季报上）：以那之前的年报 9 项为底，季报判得了的几项用季报更新（用户 2026-11-03 选的做法）。

不读 PDF：两份体检结果用 build_from_lines 的形状手写。真实报告上的效果在本地用精达、七匹狼的
2025 年报 + 2026 三季报跑过。
"""
from rag import healthcheck as hc
from rag.healthcheck import GREEN, NONE, RED, YELLOW
from ui import memory
from ui.payload import health_card
from ui.turn import health_entry, health_page_doc

BASE = {"doc": "a" * 16, "label": "七匹狼 2025 年报"}
UPDATE = {"doc": "b" * 16, "label": "七匹狼 2026 三季报"}


def _hc(period, statuses):
    items = [hc._item(k, statuses.get(k, NONE), f"v-{k}", f"s-{k}", 5) for k in hc.ORDER]
    return {"version": hc.HC_VERSION, "period": period, "items": items}


ANNUAL = _hc("FY", {k: GREEN for k in hc.ORDER} | {"profit": RED})
QUARTER = _hc("Q3", {"cash": YELLOW, "profit": YELLOW, "goodwill": NONE})      # 商誉季报里没找到


def test_quarter_updates_what_it_can_and_keeps_the_rest():
    m = hc.merge_quarter(ANNUAL, QUARTER, BASE, UPDATE)
    by = {it["key"]: it for it in m["items"]}
    assert [it["key"] for it in m["items"]] == list(hc.ORDER)
    assert (by["cash"]["status"], by["cash"]["src"], by["cash"]["doc"]) == (YELLOW, UPDATE["label"], UPDATE["doc"])
    assert by["cash"]["prev"] == {"status": GREEN, "value": "v-cash", "src": BASE["label"]}
    assert by["profit"]["prev"]["status"] == RED
    # 季报没判出来的项保留年报的结论，不被「未判断」盖掉
    assert (by["goodwill"]["status"], by["goodwill"]["src"], "prev" in by["goodwill"]) == (GREEN, BASE["label"], False)
    # 只有年报才披露的 6 项照搬年报
    assert by["audit"]["src"] == BASE["label"] and by["audit"]["doc"] == BASE["doc"]
    assert m["counts"] == {RED: 0, YELLOW: 2, GREEN: 7, NONE: 0}
    assert (m["period"], m["base"], m["update"]) == (hc.MERGED, BASE["label"], UPDATE["label"])


def test_brief_says_which_report_covers_what_and_what_changed():
    text = hc.brief(hc.merge_quarter(ANNUAL, QUARTER, BASE, UPDATE), "")
    assert text.startswith("按《七匹狼 2025 年报》做了 9 项体检，其中现金流含金量、利润含金量 2 项用《七匹狼 2026 三季报》")
    assert "**和年报相比**：现金流含金量 绿灯 → 黄灯" in text and "利润含金量 红灯 → 黄灯" in text
    assert "（七匹狼 2026 三季报第 5 页）" in text
    assert "未作判断" not in text


def test_note_goes_first_when_annual_could_not_be_fetched():
    text = hc.brief(QUARTER, "七匹狼 2026 三季报", note="本会话的报告导入次数已用完。")
    assert text.startswith("本会话的报告导入次数已用完。\n\n按《七匹狼 2026 三季报》")
    assert "只在年报里披露" in text


def test_button_question_is_still_recognised():
    q = hc.button_question("七匹狼")
    assert "财报体检" in q and hc.wants_health(q)


def test_entry_card_archive_and_page_lookup():
    m = hc.merge_quarter(ANNUAL, QUARTER, BASE, UPDATE)
    entry = health_entry({"q": "给七匹狼做个财报体检", "doc_id": UPDATE["doc"]}, m, UPDATE["label"])
    card = health_card(entry["health"])
    assert (card["base"], card["update"]) == (BASE["label"], UPDATE["label"])
    cash = next(it for it in card["items"] if it["key"] == "cash")
    assert cash["src"] == UPDATE["label"] and cash["prev"]["status"] == GREEN
    assert all("doc" not in it for it in card["items"])               # 报告 id 不下发
    # 翻原页：每项翻它自己那份报告；认不出的 key 退回这一轮依据的报告
    assert health_page_doc(entry, "audit", "x") == BASE["doc"]
    assert health_page_doc(entry, "cash", "x") == UPDATE["doc"]
    assert health_page_doc(entry, "瞎写", "x") == UPDATE["doc"]
    # 浏览器存档原样带回、能再渲染
    back = memory.clean_entry(memory.pack([entry])[0], {BASE["doc"], UPDATE["doc"]})
    assert health_card(back["health"])["items"] == card["items"]


def test_card_drops_malformed_prev_from_browser():
    raw = {"period": hc.MERGED, "base": "x", "update": "y",
           "items": [{"key": "cash", "status": GREEN, "src": "y" * 99, "prev": {"status": "purple", "value": 1}},
                     {"key": "audit", "status": GREEN, "prev": "乱写"}]}
    items = health_card(raw)["items"]
    assert "prev" not in items[0] and len(items[0]["src"]) == 40 and "prev" not in items[1]
