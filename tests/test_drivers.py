"""影响这家公司的几件大事：程序那一半（定位段落、核对引用和数字）。模型那一半见 tools/eval_drivers.py。

夹具是真实年报的原文行。核对是这个功能的底线：模型给的引用、暴露度、说明里的数字，
找不到原文出处的一律不能交给用户。
"""
from rag import drivers as dr


def _page(text: str) -> list[str]:
    return text.strip("\n").split("\n")


def _pages(*texts: str) -> list[list[str]]:
    return [_page(t) for t in texts]


FILLER = [["封面"]] * 5          # 风险章节跳过前 5 页（那里常有「详见……可能面对的风险」的引用句）

RISK = """(四)可能面对的风险
√适用 □不适用
1、原材料价格波动风险
铜、铝等大宗商品价格受国际供需、货币政策、地缘事件等多重因素影响，波动加大。
(五)其他
□适用 √不适用"""

COST = """(4). 成本分析表
单位：万元 币种：人民币
漆包线 原材料 313,620.68 98.02 308,342.72 98.27 1.71
(5). 主要销售客户及主要供应商情况
前五名客户销售额238,841.38万元，占年度销售总额9.69%"""


# ---------------------------------------------------------------- 定位段落

def test_risk_section_stops_at_next_heading():
    secs = dr.locate_sections(FILLER + _pages(RISK))
    text = secs["risk"][0][1]
    assert secs["risk"][0][0] == 6
    assert "铜、铝等大宗商品价格" in text and "(五)其他" not in text


def test_risk_reference_follows_to_important_risk_notice():
    # 牧原：风险章节只写「详见第一节“重要风险提示”」
    pages = _pages("封面", "重要风险提示\n1、发生疫病的风险\n疫病的发生将导致生猪的死亡\n第二节 公司简介")
    pages += [["正文"]] * 4
    pages += _pages("（四）可能面临的风险和采取的措施\n公司可能面临的风险具体如下：详见第一节“重要风险提示”。")
    secs = dr.locate_sections(pages)
    assert "risk_ref" in secs
    assert "疫病的发生" in secs["risk_ref"][0][1] and "第二节" not in secs["risk_ref"][0][1]


def test_risk_fallback_to_outlook():
    # 立讯没有单列风险章节，只有「公司未来发展的展望」
    secs = dr.locate_sections(FILLER + _pages("十一、公司未来发展的展望\n面对新一轮科技增长周期\n十二、其他"))
    assert secs["risk"][0][1].startswith("十一、公司未来发展的展望")


def test_cost_section_stops_before_customers_and_top5_found():
    secs = dr.locate_sections(FILLER + _pages(COST))
    assert "98.02" in secs["cost"][0][1] and "主要销售客户" not in secs["cost"][0][1]
    assert "9.69%" in secs["top5"][0][1]


def test_fx_section_only_in_later_part():
    early = _pages("（1）外汇风险\n管理层讨论里提到的外汇风险，没有敏感性数字")
    late = _pages("（1）外汇风险\n人民币对美元贬值 5 976,367,493.28\n（2）利率风险\n本公司无重大利率风险。")
    secs = dr.locate_sections(FILLER + early + [["正文"]] * 20 + late)
    assert "976,367,493.28" in secs["fx"][0][1] and "利率风险" not in secs["fx"][0][1]
    assert secs["rate"][0][1].endswith("本公司无重大利率风险。")


def test_sections_text_labels_pages_and_dedupes():
    secs = {"mix": [(16, "分产品 漆包线")], "cost": [(16, "分产品 漆包线")], "risk": [(28, "铜价波动")]}
    text = dr.sections_text(secs)
    assert text.count("〔第 16 页〕") == 1 and "【风险章节】" in text and "〔第 28 页〕" in text


# ---------------------------------------------------------------- 核对

PAGES = FILLER + _pages(RISK, COST)     # 风险在第 6 页，成本在第 7 页


def _raw(**kw):
    d = {"type": "成本", "name": "铜价", "how": "铜价上涨推高成本。",
         "evidence": [{"page": 6, "quote": "铜、铝等大宗商品价格受国际供需"}],
         "exposure": {"page": 7, "text": "原材料占漆包线成本的98.02%", "value": "98.02%"},
         "market": "cu"}
    d.update(kw)
    return d


def test_verify_keeps_grounded_driver():
    out = dr.verify([_raw()], PAGES)
    assert out[0]["evidence"][0]["page"] == 6
    assert out[0]["exposure"]["value"] == "98.02%"
    assert out[0]["market"] == "cu"


def test_quote_matches_across_line_breaks_and_neighbor_page():
    # 原文在两行之间断开；页码报错一页也能找到，记成实际所在页
    out = dr.verify([_raw(evidence=[{"page": 7, "quote": "地缘事件等多重因素影响，波动加大"}])], PAGES)
    assert out[0]["evidence"][0]["page"] == 6


def test_driver_without_any_real_quote_is_dropped():
    fake = _raw(evidence=[{"page": 6, "quote": "铜价今年预计上涨 30%"}])
    assert dr.verify([fake], PAGES) == []


def test_exposure_number_not_on_page_is_dropped():
    out = dr.verify([_raw(exposure={"page": 7, "text": "原材料占成本 95%", "value": "95%"})], PAGES)
    assert out[0]["exposure"] is None
    # 说明文字里冒出 value 以外的数字也不行
    out = dr.verify([_raw(exposure={"page": 7, "text": "占比 98.02%，同比上升 3 个点", "value": "98.02%"})], PAGES)
    assert out[0]["exposure"] is None


def test_how_clause_with_unverified_number_removed():
    how = "铜价上涨推高成本，原材料占成本98.02%，预计影响净利润约6,780.03万元。"
    out = dr.verify([_raw(how=how)], PAGES, dr.locate_sections(PAGES))
    assert "98.02%" in out[0]["how"] and "6,780.03" not in out[0]["how"]


def test_unknown_market_and_type_normalized():
    out = dr.verify([_raw(market="LME铜", type="宏观")], PAGES)
    assert out[0]["market"] == "" and out[0]["type"] == "其他"


def test_at_most_five_drivers():
    assert len(dr.verify([_raw(name=f"因素{i}") for i in range(8)], PAGES)) == dr.MAX_DRIVERS


def test_dce_products_marked_for_search():
    # 大商所公开页面有访问校验，不去绕开：它的品种走联网搜索
    assert dr.MARKETS["lh"][1] == "search" and dr.MARKETS["cu"][1] == "shfe"


# ---------------------------------------------------------------- 模型调用

class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class _Client:
    """按顺序吐出预设的回复；异常对象就抛出。"""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kw):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return _Resp(r)


def test_ask_model_retries_once_after_failure():
    ok = '{"drivers": [{"name": "铜价"}]}'
    c = _Client(TimeoutError("超时"), ok)
    assert dr.ask_model({"risk": [(6, "铜价")]}, client=c) == [{"name": "铜价"}] and c.calls == 2
    # 推理吃光 token：正文为空也算失败，重试一次后放弃
    c = _Client("", "")
    assert dr.ask_model({"risk": [(6, "铜价")]}, client=c) is None and c.calls == 2


# ---------------------------------------------------------------- 第 3 步：最新变化

from datetime import date  # noqa: E402

TODAY = date(2026, 11, 2)


def test_clean_news_rules():
    text = """【1】
- 2026-10-27 全国生猪均价 10.71 元/公斤，连续 7 周下跌[网1]
- 2026-10-30 瘦肉型猪出栏均价 9.04 元/公斤 [网9]
- 全国猪价下跌[网2]
- 2025-12-01 一年前的消息[网2]
- 2026-10-31 第三条超出每个因素的上限[网3]
【2】
- 未找到近期可核实的信息
【7】
- 2026-10-01 编号不存在的因素[网1]"""
    out = dr.clean_news(text, n_drivers=2, n_web=3, today=TODAY)
    # 第 2 条引用了不存在的 [网9]、第 3 条没日期、第 4 条超过 6 个月 —— 都删；每个因素最多 2 条
    assert out == {1: ["2026-10-27 全国生猪均价 10.71 元/公斤，连续 7 周下跌[网1]",
                       "2026-10-31 第三条超出每个因素的上限[网3]"]}


def test_clean_news_markdown_link_citation():
    out = dr.clean_news("【1】\n- 2026-10 玻纤提价[网2](https://x.com/a)", 1, 2, TODAY)
    assert out[1] == ["2026-10 玻纤提价[网2]"]


def test_latest_changes_routes_data_search_and_customers(monkeypatch):
    found = {"drivers": [
        {"type": "成本", "name": "铜价", "market": "cu", "how": ""},
        {"type": "售价", "name": "生猪价格", "market": "lh", "how": ""},     # 大商所：联网
        {"type": "政策", "name": "药品集采", "market": "", "how": ""},
        {"type": "客户", "name": "大客户", "market": "", "how": ""},        # 不搜
        {"type": "汇率", "name": "美元汇率", "market": "USD", "how": ""},   # 官方数据取不到：改联网
    ]}
    quotes = {"cu": {"name": "铜", "change_pct": 1.74}, "USD": None}
    monkeypatch.setattr(dr.market, "quote", lambda code, name, src, today=None: quotes.get(code))
    asked = {}

    def fake_search(targets, **kw):
        asked["names"] = [d["name"] for d in targets]
        return {1: ["2026-10-27 猪价下跌[网1]"]}, [{"n": 1, "url": "https://a"}]
    monkeypatch.setattr(dr, "search_news", fake_search)
    out = dr.latest_changes(found, client=None, api_key="k", today=TODAY)
    by = {d["name"]: d for d in out["drivers"]}
    assert asked["names"] == ["生猪价格", "药品集采", "美元汇率"]
    assert by["铜价"]["now"]["change_pct"] == 1.74 and by["铜价"]["news"] == []
    assert by["生猪价格"]["news"] == ["2026-10-27 猪价下跌[网1]"]
    assert by["大客户"]["now"] is None and "定期报告" in by["大客户"]["note"]
    assert out["date"] == "2026-11-02" and out["web_sources"][0]["n"] == 1
    assert "now" not in found["drivers"][0]          # 不改传进来的结果（它是缓存里的东西）


def test_search_news_skipped_without_key_or_targets():
    assert dr.search_news([], client=None, api_key="k") == ({}, [])
    assert dr.search_news([{"name": "x", "market": ""}], client=None, api_key="") == ({}, [])


def test_market_code_must_match_what_the_driver_says():
    # 实测：时代新材「玻纤、树脂等原材料价格」被配上天然橡胶（ru）—— 对不上就去掉代码
    out = dr.verify([_raw(name="玻纤、树脂等原材料价格", how="原材料涨价推高成本。", market="ru")], PAGES)
    assert out[0]["market"] == ""
    # 词出现在原文引用里也算（这里引用里有「铜」）
    out = dr.verify([_raw(name="大宗商品价格", how="涨价推高成本。", market="cu")], PAGES)
    assert out[0]["market"] == "cu"


def test_every_market_code_has_match_words():
    assert set(dr.MARKET_WORDS) == set(dr.MARKETS)
