"""多年趋势：两份年报的主要会计数据接成六年，指标卡改画折线。

数字是精达股份 2025 年报（第 7 页）和 2022 年报（第 6 页）里的真实值，
2026-11-02 从巨潮下载原文程序抽取、逐份通过增减复算。
"""
import json

import pytest

from rag import config, history
from rag.history import load_older, merge, older_year, wants_trend
from ui.card import build_card

Y = 1e8


def _fin(years, values, page):
    rev, np_ = values
    return {"version": 2, "period": "FY", "unit": "元", "page": page, "years": years,
            "items": {
                "revenue": {"label": "营业收入", "change": 10.53,
                            "values": dict(zip(years, [v * Y for v in rev]))},
                "np_parent": {"label": "归属于上市公司股东的净利润", "change": 6.66,
                              "values": dict(zip(years, [v * Y for v in np_]))},
            }}


def _new():
    return _fin(["2025", "2024", "2023"], ([246.74, 223.23, 179.06], [5.99, 5.62, 4.26]), 7)


def _old():
    return _fin(["2022", "2021", "2020"], ([175.42, 183.30, 124.47], [3.81, 5.49, 4.19]), 6)


@pytest.mark.parametrize("q", ["精达股份近几年营收趋势", "过去五年利润走势如何",
                               "近5年净利润", "历年营收变化", "长期来看盈利能力怎么样"])
def test_trend_questions(q):
    assert wants_trend(q)


@pytest.mark.parametrize("q", ["2025年营收多少", "利润率变化如何", "前五大客户是谁"])
def test_not_trend_questions(q):
    assert not wants_trend(q)


def test_short_followup_inherits_trend():
    assert wants_trend("那净利润呢", previous=["近几年营收趋势"])
    assert not wants_trend("那净利润呢", previous=["2025年营收多少"])


def test_older_year_is_one_before_earliest():
    assert older_year(_new()) == 2022
    assert older_year({}) is None


def test_merge_six_years_and_sources():
    m = merge(_new(), _old())
    assert m["years"] == ["2025", "2024", "2023", "2022", "2021", "2020"]
    assert m["items"]["revenue"]["values"]["2020"] == pytest.approx(124.47 * Y)
    assert m["items"]["revenue"]["change"] == 10.53          # 增减仍是本年披露值
    assert m["sources"] == [
        {"years": "2023–2025", "label": "2025 年报", "page": 7},
        {"years": "2020–2022", "label": "2022 年报", "page": 6},
    ]
    assert "sources" not in _new()                            # 不改入参


def test_merge_newer_report_wins_on_overlap():
    old = _fin(["2023", "2022", "2021"], ([170.0, 175.42, 183.30], [4.0, 3.81, 5.49]), 6)
    m = merge(_new(), old)
    assert m["items"]["revenue"]["values"]["2023"] == pytest.approx(179.06 * Y)
    assert m["years"] == ["2025", "2024", "2023", "2022", "2021"]


def test_merge_refuses_gap_and_non_annual():
    gap = _fin(["2021", "2020", "2019"], ([1, 2, 3], [1, 2, 3]), 6)
    assert merge(_new(), gap) is not None and merge(_new(), gap)["years"] == _new()["years"]
    q3 = dict(_new(), period="Q3")
    assert merge(q3, _old()) is q3
    assert merge(_new(), None)["years"] == _new()["years"]


def test_card_draws_line_with_cagr():
    card = build_card(merge(_new(), _old()))
    rev = card["charts"][0]
    assert rev["kind"] == "line"
    assert rev["labels"] == ["2020", "2021", "2022", "2023", "2024", "2025"]
    assert rev["values"] == [124.47, 183.3, 175.42, 179.06, 223.23, 246.74]
    # (246.74 / 124.47) ** (1/5) - 1 = 14.67%
    assert rev["cagr"] == "5 年复合增速 +14.67%"
    assert card["cardTitle"] == "主要会计数据 · 2025 · 近 6 年"
    assert card["cardNote"] == ("程序抽取 · 2023–2025 出自 2025 年报第 7 页 · "
                                "2020–2022 出自 2022 年报第 6 页 · 均已与披露增减核对")


def test_three_years_stay_bars():
    card = build_card(_new())
    assert all("kind" not in c for c in card["charts"])
    assert card["cardTitle"] == "主要会计数据 · 2025"


def test_item_missing_older_years_falls_back_to_three():
    old = _old()
    del old["items"]["np_parent"]                    # 往年表里归母净利润没通过复算
    card = build_card(merge(_new(), old))
    rev, np_ = card["charts"]
    assert rev["kind"] == "line" and len(np_["values"]) == 3 and "kind" not in np_


def test_no_cagr_when_endpoint_negative():
    old = _fin(["2022", "2021", "2020"], ([175.42, 183.30, 124.47], [3.81, 5.49, -1.0]), 6)
    np_ = build_card(merge(_new(), old))["charts"][1]
    assert np_["kind"] == "line" and "cagr" not in np_


# ---------------------------------------------------------------- 取数顺序与缓存

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def test_downloads_once_then_uses_cache(tmp_data, monkeypatch):
    calls = []
    monkeypatch.setattr(history, "_from_cninfo", lambda c, y, p="FY": calls.append(y) or _old())
    fin, fetched = load_older("精达股份", 2022, [], now=1000)
    assert fetched and fin["years"][0] == "2022"
    fin2, fetched2 = load_older("精达股份", 2022, [], now=2000)
    assert not fetched2 and fin2 == fin and calls == [2022]
    saved = json.loads((tmp_data / "_history" / "精达_2022.json").read_text(encoding="utf-8"))
    assert saved["fin"]["page"] == 6


def test_miss_is_cached_for_a_week(tmp_data, monkeypatch):
    calls = []
    monkeypatch.setattr(history, "_from_cninfo", lambda c, y, p="FY": calls.append(y))
    assert load_older("新上市", 2022, [], now=0) == (None, True)
    assert load_older("新上市", 2022, [], now=3600) == (None, False)
    assert load_older("新上市", 2022, [], now=history.MISS_TTL + 1) == (None, True)
    assert calls == [2022, 2022]


def test_network_error_not_cached(tmp_data, monkeypatch):
    def boom(c, y, p="FY"):
        raise RuntimeError("下载超时")
    monkeypatch.setattr(history, "_from_cninfo", boom)
    with pytest.raises(RuntimeError):
        load_older("精达股份", 2022, [])
    assert not (tmp_data / "_history" / "精达_2022.json").exists()


def test_no_fetch_when_session_budget_used(tmp_data, monkeypatch):
    monkeypatch.setattr(history, "_from_cninfo", lambda c, y, p="FY": pytest.fail("不该下载"))
    assert load_older("精达股份", 2022, [], allow_fetch=False) == (None, False)


def test_local_report_preferred(tmp_data, monkeypatch):
    monkeypatch.setattr(history, "_from_cninfo", lambda c, y, p="FY": pytest.fail("不该下载"))
    monkeypatch.setattr(history, "load_or_build", lambda doc_id: _old())
    docs = [{"doc_id": "a" * 16, "company": "精达股份", "year": "2022"}]
    fin, fetched = load_older("精达", 2022, docs)
    assert not fetched and fin["page"] == 6 and fin["source"] == "本地"


# ---------------------------------------------------------------- 第二步：往年数字进回答

def test_as_hit_from_cninfo_cache():
    hit = history.as_hit(_old(), "精达股份")
    assert hit["doc_id"] == "hist:精达_2022" and history.is_hist_id(hit["doc_id"])
    assert hit["note"] is True and hit["note_src"] == "精达股份 2022 年报"
    assert hit["section"] == "往年年报 · 精达股份 2022 年报"
    assert hit["page_start"] == hit["page_end"] == 6
    assert hit["text"].splitlines()[0] == ("营业收入：2022 年 175.42 亿元；2021 年 183.30 亿元；"
                                           "2020 年 124.47 亿元；本期比上年同期增减 +10.53%")


def test_as_hit_local_report_keeps_its_doc_id():
    assert history.as_hit(dict(_old(), doc_id="a" * 16), "精达股份")["doc_id"] == "a" * 16
    assert history.as_hit(None, "精达股份") is None
    assert history.as_hit(dict(_old(), items={}), "精达股份") is None


def test_attach_replaces_same_page_note():
    hit = history.as_hit(_old(), "精达股份")
    old_note = {"doc_id": "hist:精达_2022", "page_start": 6, "text": "营业收入：2022 年 175.42 亿元"}
    other = {"doc_id": "b" * 16, "page_start": 6, "text": "x"}
    assert history.attach([other, old_note], hit) == [other, hit]
    assert history.attach([other], None) == [other]


def test_page_png_only_serves_valid_ids(tmp_data):
    (tmp_data / "_history").mkdir()
    (tmp_data / "_history" / "精达_2022_p6.png").write_bytes(b"png")
    assert history.page_png("hist:精达_2022", 6) == b"png"
    assert history.page_png("hist:精达_2022", 7) is None
    for bad in ("hist:../x_2022", "hist:精达_22", "a" * 16, "", None):
        assert history.page_png(bad, 6) is None


def test_cninfo_fetch_saves_table_page(tmp_data, monkeypatch):
    from rag import disclosure, pdf_parser

    monkeypatch.setattr(disclosure, "find_report", lambda c, y, p: {"url": "https://static.cninfo.com.cn/x.pdf"})
    monkeypatch.setattr(disclosure, "download_pdf", lambda url, timeout: (b"%PDF", {}))
    monkeypatch.setattr(history, "extract", lambda data, period, year: _old())
    monkeypatch.setattr(pdf_parser, "render_page_png", lambda data, page: f"page{page}".encode())
    fin, fetched = load_older("精达股份", 2022, [], now=0)
    assert fetched and fin["page"] == 6
    assert history.page_png(history.as_hit(fin, "精达股份")["doc_id"], 6) == b"page6"


def test_cache_without_shot_refetches_once(tmp_data, monkeypatch):
    """第二步之前写的缓存没有表所在页截图：能下载就重下一次补上，额度用完就先用缓存里的数。"""
    path = tmp_data / "_history" / "精达_2022.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": history.FIN_VERSION, "at": 0, "fin": _old()}), encoding="utf-8")
    monkeypatch.setattr(history, "_from_cninfo", lambda c, y, p="FY": pytest.fail("不该下载"))
    assert load_older("精达股份", 2022, [], allow_fetch=False) == (_old(), False)
    calls = []
    monkeypatch.setattr(history, "_from_cninfo", lambda c, y, p="FY": calls.append(y) or _old())
    assert load_older("精达股份", 2022, [], now=10)[1] is True
    assert load_older("精达股份", 2022, [], now=20)[1] is False and calls == [2022]


def test_answer_rows_from_history_hit_keep_hist_source():
    from rag.notes import as_hits, from_answer

    hit = history.as_hit(_old(), "精达股份")
    answer = ("| 年份 | 营业收入 |\n|---|---|\n| 2021 年 | 183.30 亿元 [2] |\n")
    notes = from_answer(answer, [{"doc_id": "c" * 16, "page_start": 7, "text": "无关"}, hit], [],
                        "c" * 16, "精达股份 2025 年报")
    assert notes == [{"doc": "hist:精达_2022", "src": "精达股份 2022 年报", "p": 6,
                      "t": "2021 年：营业收入 183.30 亿元"}]
    # 下一轮带回来时翻原页仍然走截图
    assert as_hits([{"notes": notes}], "c" * 16)[0]["doc_id"] == "hist:精达_2022"


def test_browser_archive_keeps_hist_sources():
    from ui.memory import _clean_hit, _clean_note

    hit = history.as_hit(_old(), "精达股份")
    assert _clean_hit(hit, set())["doc_id"] == "hist:精达_2022"
    note = {"doc": "hist:精达_2022", "src": "精达股份 2022 年报", "p": 6, "t": "营业收入：2021 年 183.30 亿元"}
    assert _clean_note(note, set()) == note
    assert _clean_note(dict(note, doc="hist:../../etc_2022"), set()) is None
