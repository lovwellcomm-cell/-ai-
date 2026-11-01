"""行业动态（rag/industry.py）：什么时候触发、模型输出怎么过滤、回合怎么走。

外部信息最容易出两种错：没有出处的「行业常识」，和过时的旧闻被当成「最新」。
所以每条都要有效的 [网n] 和日期，超过 12 个月的删掉；关联公司的判断必须同时引用报告和网页。
"""
from datetime import date

import pytest

from rag import external, industry
from rag.external import local_date
from tests.test_external import FakeClient, text_msg, tool_call
from ui.turn import step

TODAY = date(2026, 11, 2)
HITS = [{"text": "公司主要原材料为铜杆", "page_start": 20, "page_end": 20,
         "section": "第三节", "subsection": "所处行业情况"}]


@pytest.mark.parametrize("q", [
    "精达股份所处行业的最新动态：原材料价格、竞争格局、同行进展",
    "行业最新情况怎么样", "最近行业有什么变化", "原材料价格走势如何",
    "同行业其他公司有什么突破", "竞争对手最近有什么动态",
])
def test_wants_industry(q):
    assert industry.wants_industry(q)


@pytest.mark.parametrize("q", [
    "营业收入是多少", "公司所处行业情况", "主要原材料是什么", "前五大客户集中度",
])
def test_report_questions_stay_on_the_report(q):
    """「所处行业情况」「主要原材料是什么」年报里就有，不该去联网。"""
    assert not industry.wants_industry(q)


def test_button_question_is_recognised():
    assert industry.wants_industry(industry.button_question("精达股份"))


RAW = """**原材料价格**
- 2026-10-15 佛山光亮铜价格 93400 元/吨[网1]
- 铜价近期高位运行[网1]
- 2024-03 铜价 70000 元/吨[网1]
**行业格局与挑战**
- 2026-10 扁线产能扩张加快[网9]
**同行动态**
- 2026-10-30 金杯电工前三季度营收增 27%[网2]
**与公司的关联**
- 公司主要原材料为铜杆[1]，铜价高位需关注成本压力[网1]
- 需关注同行扩产[网2]
"""


def test_clean_keeps_only_dated_recent_and_cited_items():
    text, status = industry.clean(RAW, n_web=2, n_doc=1, today=TODAY)
    assert status == "trimmed"
    assert text.splitlines() == [
        "**原材料价格**",
        "- 2026-10-15 佛山光亮铜价格 93400 元/吨[网1]",   # 没日期的、两年前的都删了
        "**行业格局与挑战**",
        f"- {industry.NOTHING_FOUND}",                    # [网9] 不存在
        "**同行动态**",
        "- 2026-10-30 金杯电工前三季度营收增 27%[网2]",
        "**与公司的关联**",
        "- 公司主要原材料为铜杆[1]，铜价高位需关注成本压力[网1]",   # 只有网页没有报告的删了
    ]


def test_clean_unwraps_markdown_links():
    raw = "**原材料价格**\n- 2026-08-25 铜价高位震荡「[网1](http://example.com/a?id=1#2)」"
    text, _ = industry.clean(raw, n_web=1, n_doc=0, today=TODAY)
    assert text.splitlines()[1] == "- 2026-08-25 铜价高位震荡[网1]"


def test_clean_with_nothing_grounded():
    assert industry.clean("**原材料价格**\n- 铜价上涨", 0, 1, TODAY) == ("", "ungrounded")


def test_local_date_converts_utc_to_beijing():
    assert local_date("2026-10-29T16:00:00+00:00") == "2026-10-30"
    assert local_date("2026-10-01") == "2026-10-01"
    assert local_date(None) is None


def _search(self, q, count=None):
    return [{"title": "扁线铜价格一览表", "url": "https://example.com/cu",
             "summary": "10月15日，佛山市场光亮铜价格93400元/吨", "site_name": "我的钢铁网",
             "date": "2026-10-15T08:00:00+00:00"}]


def test_industry_brief_searches_and_formats(monkeypatch):
    monkeypatch.setattr(external.SearchClient, "search", _search)
    answer = ("**原材料价格**\n- 2026-10-15 佛山光亮铜价格 93400 元/吨[网1]\n"
              "**行业格局与挑战**\n- 未找到近期可核实的信息\n"
              "**同行动态**\n- 未找到近期可核实的信息")
    client = FakeClient([tool_call("search_web", {"query": "铜价 2026年10月"}),
                         text_msg(answer)])
    text, sources, diag = industry.industry_brief("行业最新动态", HITS, client=client,
                                                  company="精达股份", year="2025", today=TODAY)
    assert text.startswith(industry.INDUSTRY_HEADING)
    assert "93400" in text and sources[0]["date"] == "2026-10-15"
    assert diag["web"] == "ok"
    system = client.calls[0]["messages"][0]["content"]
    assert "今天是 2026-11-02" in system
    assert "所处行业情况" in client.calls[0]["messages"][1]["content"]   # 报告片段带进去了


def test_industry_brief_says_so_when_nothing_found(monkeypatch):
    client = FakeClient([text_msg("铜价应该在涨。")])
    text, sources, diag = industry.industry_brief("行业最新动态", HITS, client=client,
                                                  today=TODAY)
    assert industry.NOTHING_FOUND in text and diag["web"] == "ungrounded"


# ---------------------------------------------------------------- 回合

def test_industry_turn_skips_answer_and_analysis():
    turn = {"q": "行业最新动态", "hits": HITS, "queries": [], "status": "generating",
            "kind": "industry", "doc_id": "d"}
    src = [{"n": 1, "title": "t", "url": "https://example.com", "site": None, "date": "2026-10-15"}]

    def ind(q, hits, **kw):
        return "**行业动态**\n- x[网1]", src, {"web": "ok"}

    def never(*a, **kw):
        raise AssertionError("行业动态回合不应调用年报回答或扩展分析")

    new, entry, err = step(turn, gen=never, ana=never, ind=ind)
    assert err is None and new["status"] == "done"
    assert entry["kind"] == "industry" and entry["web_sources"] == src
    assert entry["a"].startswith("**行业动态**")


def test_industry_turn_failure_is_an_error():
    turn = {"q": "行业最新动态", "hits": [], "queries": [], "status": "generating",
            "kind": "industry"}

    def boom(*a, **kw):
        raise RuntimeError("HTTP 429")

    _, entry, err = step(turn, gen=boom, ana=boom, ind=boom)
    assert entry is None and "HTTP 429" in err
