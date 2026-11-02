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


# ---------------------------------------------------------------- 第 4 步：暴露度金额与估算

AMOUNT_PAGE = FILLER + _pages(RISK, COST)     # 第 7 页「单位：万元」，原材料 313,620.68


def test_exposure_text_may_mention_verified_amount():
    # 实测模型常把金额也写进说明：只要金额本身核对过，就不该把整条暴露度删掉
    ex = {"page": 7, "text": "原材料 313,620.68 万元，占成本 98.02%", "value": "98.02",
          "amount": {"value": "313,620.68", "unit": "万元"}}
    stats = {}
    out = dr.verify([_raw(exposure=ex)], AMOUNT_PAGE, stats=stats)
    assert out[0]["exposure"]["amount"] == {"value": "313,620.68", "unit": "万元", "yuan": 3136206800.0}
    assert stats == {}


def test_amount_unit_must_match_page_declaration():
    # 这一页声明的是「单位：万元」，模型说成「元」差一万倍 —— 模型的金额不要；
    # 程序按这一页声明的单位重新取（98.02 前面紧挨着的 313,620.68 万元）
    ex = {"page": 7, "text": "原材料占成本 98.02%", "value": "98.02",
          "amount": {"value": "313,620.68", "unit": "元"}}
    out = dr.verify([_raw(exposure=ex)], AMOUNT_PAGE)
    assert out[0]["exposure"]["amount"]["unit"] == "万元" and out[0]["exposure"]["amount"]["inferred"]


def test_dropped_exposure_counted():
    stats = {}
    ex = {"page": 7, "text": "原材料占成本 50%", "value": "50%"}
    dr.verify([_raw(exposure=ex)], AMOUNT_PAGE, stats=stats)
    assert stats["exposure_dropped"] == 1 and stats["dropped"][0]["why"] == "数字不在该页"


def test_short_numbers_need_whole_number_match():
    # 「3」不能因为页面上有「313,620.68」就算找到；「1.71」作为完整的数在第 7 页
    assert not dr._has_number(dr._spaced("原材料 313,620.68 98.02"), "3")
    assert dr._has_number(dr._spaced("308,342.72 98.27 1.71"), "1.71")
    assert dr._has_number(dr._spaced("占比 98.02%"), "98.02")
    # PDF 把一个数拆开：「1,613,888. 96」接回去
    assert dr._has_number(dr._spaced("漆包线 原材料\n1,613,888. 96\n95.45"), "1,613,888.96")
    # 长数字跨行拆开（「2,029,856.2」换行「6」）：去空白后能找到
    assert dr._number_in(dr._spaced("2,029,856.2\n6"), dr._squash("2,029,856.2\n6"), "2,029,856.26")


FIN = {"years": ["2025"], "items": {
    "revenue": {"values": {"2025": 2.467e10}}, "np_parent": {"values": {"2025": 5.99e8}},
    "total_assets": {"values": {"2025": 1.45e10}}}}


def _driver(kind, now, yuan, name="原材料价格"):
    return {"type": kind, "name": name, "now": now, "news": [],
            "exposure": {"amount": {"yuan": yuan}} if yuan else None}


def test_estimate_cost_rise_hurts_profit():
    e = dr.estimate(_driver("成本", {"name": "铜", "change_pct": 1.74}, 1.6138889600e10), FIN)
    assert e["status"] == "ok"
    assert e["amount"] == round(-1.6138889600e10 * 0.0174, 2)
    assert e["share"] == round(e["amount"] / 5.99e8 * 100, 1)       # 约 −46.9%
    assert e["formula"].startswith("−这项成本 161.39 亿元 × 铜 +1.74%")
    assert "不能转嫁" in e["assume"] and "上限" in e["assume"]


def test_estimate_price_fall_and_sensitivity_attached():
    e = dr.estimate(_driver("售价", {"name": "碳酸锂", "change_pct": -34.52}, 5.69e9), FIN)
    assert e["status"] == "ok" and e["amount"] < 0 and e["share_of"] == "归母净利润"
    assert e["sensitivity"]["amount"] == round(5.69e9 * 0.10, 2)


def test_rate_unchanged_gives_sensitivity_per_10bp():
    fin = {"years": ["2025"], "items": {"total_assets": {"values": {"2025": 5.8e11}},
                                        "np_parent": {"values": {"2025": 3.45e10}}}}   # 长江电力的量级
    e = dr.estimate(_driver("利率", {"name": "5年期以上LPR", "change_bp": 0}, 2.34159e11), fin)
    assert e["status"] == "sensitivity" and e["why"] == "近 3 个月没有变化"
    assert e["sensitivity"]["amount"] == round(-2.34159e11 * 0.001, 2)        # 每上升 10bp 约少 2.34 亿
    assert e["sensitivity"]["text"].startswith("利率每上升 10 个基点，利润约减少 2.34 亿元")


def test_no_official_data_still_gives_sensitivity():
    # 牧原：生猪是大商所品种，没有官方数据；但收入金额核对过，敏感度照样能给
    d = _driver("售价", None, 1.4020717687234e11)
    d["news"] = ["2026-10-27 猪价下跌[网1]"]
    fin = {"years": ["2025"], "items": {"revenue": {"values": {"2025": 1.44e11}},
                                        "np_parent": {"values": {"2025": 1.5487e10}}}}
    e = dr.estimate(d, fin)
    assert e["status"] == "sensitivity" and "联网信息只作参考" in e["why"]
    assert e["sensitivity"]["share"] == round(1.4020717687234e10 / 1.5487e10 * 100, 1)   # 约 90.5%


def test_fx_share_is_of_revenue_not_profit():
    e = dr.estimate(_driver("汇率", {"name": "美元兑人民币中间价", "change_pct": -1.11}, 2.0e10), FIN)
    assert e["share_of"] == "营业收入"
    assert e["share"] == round(-2.0e10 * 0.0111 / 2.467e10 * 100, 1)


def test_default_market_for_rate_and_fx():
    assert dr.default_market("利率", "长期借款主要为浮动利率") == "LPR5Y"
    assert dr.default_market("利率", "银行借款利率上升") == "LPR1Y"
    assert dr.default_market("汇率", "欧元结算") == "EUR"
    assert dr.default_market("汇率", "人民币兑外币汇率") == "USD"
    assert dr.default_market("成本", "铜价") == ""


def test_estimate_refuses_without_amount_or_sane_scale():
    assert "没有找到这项收入" in dr.estimate(
        {"type": "售价", "name": "生猪价格", "now": None, "news": ["x"]}, FIN)["why"]
    assert "没有找到" in dr.estimate(_driver("成本", {"name": "铜", "change_pct": 1.0}, None), FIN)["why"]
    # 成本比全年营收还大：多半是单位读错，不算
    assert "对不上" in dr.estimate(_driver("成本", {"name": "铜", "change_pct": 1.0}, 3e10), FIN)["why"]
    assert dr.estimate({"type": "政策", "now": None}, FIN)["status"] == "none"


def test_sensitivity_wording():
    d = _driver("成本", {"name": "铜", "change_pct": 1.74}, 1.6e10)
    d["name"] = "铜价"
    assert dr.estimate(d, FIN)["sensitivity"]["text"].startswith("铜价格每上升 10%，利润约减少 16.00 亿元")
    fx = _driver("汇率", {"name": "美元兑人民币中间价", "change_pct": -1.11}, 2.0e10)
    assert dr.estimate(fx, FIN)["sensitivity"]["text"].startswith("外币每升值 10%（人民币贬值），境外收入折算约增加")


def test_concentration_mistyped_as_cost_not_estimated():
    d = _driver("成本", None, 1.1e10)
    d["name"] = "供应商集中（供应商1）"
    assert dr.estimate(d, FIN)["status"] == "none"


# ---------------------------------------------------------------- 第 5 步：回合与卡片

from ui import memory as chat_memory, payload, turn as turn_core      # noqa: E402


import pytest  # noqa: E402


@pytest.mark.parametrize("q,want", [
    ("影响这家公司业绩的几件大事", True),
    ("什么因素对精达影响最大？", True),
    ("铜价涨价对公司有什么影响", True),
    ("最近有什么变化会影响业绩", True),
    ("营业收入是多少？", False),
    ("给精达做个年报体检", False),
])
def test_wants_drivers(q, want):
    assert dr.wants_drivers(q) is want


def test_button_question_recognized():
    assert dr.wants_drivers(dr.button_question("精达股份"))


def _found_latest():
    d = {"type": "成本", "name": "铜价", "how": "铜价上涨推高成本。", "market": "cu",
         "evidence": [{"page": 28, "quote": "铜、铝等大宗商品价格受国际供需"}],
         "exposure": {"page": 16, "text": "漆包线原材料占成本 95.45%", "value": "95.45",
                      "amount": {"value": "1,613,888.96", "unit": "万元", "yuan": 1.613888896e10}}}
    now = {"code": "cu", "name": "铜", "source": "上海期货交易所", "basis": "主力合约结算价", "unit": "元/吨",
           "latest": {"date": "2026-11-02", "contract": "cu2612", "value": 107580.0},
           "base": {"date": "2026-08-03", "contract": "cu2610", "value": 105740.0}, "change_pct": 1.74}
    cust = {"type": "客户", "name": "大客户", "how": "", "market": "", "evidence": [{"page": 17, "quote": "前五名"}],
            "exposure": None}
    found = {"period": "FY", "drivers": [d, cust]}
    latest = {"date": "2026-11-02", "web_sources": [],
              "drivers": [dict(d, now=now, news=[]), dict(cust, now=None, news=[], note="以定期报告披露为准")]}
    return found, latest


def test_card_and_brief():
    found, latest = _found_latest()
    c = dr.card(found, latest, FIN)
    first = c["items"][0]
    assert first["estimate"]["status"] == "ok" and first["now"]["change_pct"] == 1.74
    assert "amount" not in (first["exposure"] or {})            # 换算细节不下发
    text = dr.brief(c, "精达股份 2025 年报")
    assert text.startswith("从《精达股份 2025 年报》里找出 2 件")
    assert "**铜价**：近 3 个月的变化若持续一年，利润约减少 2.81 亿元，相当于去年归母净利润的 46.9%" in text
    assert "不构成投资建议" in text and "截至 2026-11-02" in text


def test_drivers_entry_payload_and_memory_round_trip():
    found, latest = _found_latest()
    c = dr.card(found, latest, FIN)
    e = turn_core.drivers_entry({"q": "几件大事", "doc_id": "a" * 16}, c, "精达股份 2025 年报",
                                [{"n": 1, "title": "t", "url": "https://x.com", "site": "x", "date": "2026-10-27"}])
    assert e["kind"] == "drivers" and e["web_sources"][0]["n"] == 1
    msg = payload.ai_message(e)
    assert msg["drivers"]["items"][0]["estimate"]["formula"].startswith("−这项成本")
    assert msg["webSources"][0]["url"] == "https://x.com"
    back = chat_memory.clean_entry(chat_memory.pack([e])[0], {"a" * 16})
    assert back["kind"] == "drivers" and back["drivers"]["items"][1]["note"] == "以定期报告披露为准"


def test_payload_sanitizes_restored_drivers_card():
    raw = {"date": "2026-11-02" * 5, "items": [
        {"name": "x" * 99, "evidence": [{"page": "3", "quote": "q"}, {"page": 5, "quote": "q" * 500}],
         "now": {"name": "铜", "change_pct": "1.7", "latest": {"date": "2026-11-02", "value": "1"}},
         "estimate": {"status": "evil", "share": True}},
        "not a dict"]}
    c = payload.drivers_card(raw)
    it = c["items"][0]
    assert len(c["items"]) == 1 and len(c["date"]) == 10 and len(it["name"]) == 30
    assert it["evidence"] == [{"page": 5, "quote": "q" * 120}]          # 页码不是整数的丢掉
    assert it["now"]["change_pct"] is None and it["now"]["latest"]["value"] is None
    assert it["estimate"]["status"] == "none" and it["estimate"]["share"] is None


def test_drivers_prefer_annual_report():
    docs = [{"doc_id": "fy", "company": "精达股份", "year": "2025", "period": "FY"},
            {"doc_id": "q3", "company": "精达股份", "year": "2026", "period": "Q3"}]
    u = {"intent": "drivers", "target": {"company": "", "year": None, "period": None}}
    plan = turn_core.plan_for(u, current_company="精达股份", current_year="2026", current_period="Q3",
                              docs=docs, today=TODAY)
    assert plan == {"action": "switch", "doc": docs[0]}


def test_amount_inferred_from_number_before_percentage():
    # 精达第 16 页：「漆包线 原材料 1,613,888. 96 95.45」—— 模型没给金额，程序取紧挨在 95.45 前面的那个数
    pages = FILLER + _pages("(4). 成本分析表\n单位：万元 币种：人民币\n漆包线 原材料\n1,613,888. 96\n95.45\n"
                            "1,442,670.1\n3\n95.77 11.87")
    both = ([dr._squash("".join(l)) for l in pages], [dr._spaced(" ".join(l)) for l in pages])
    a = dr.amount_before(both, 6, "95.45")
    assert a == {"value": "1,613,888.96", "unit": "万元", "yuan": 16138889600.0, "inferred": True}
    assert dr.amount_before(both, 6, "120") is None                     # 不是百分比
    two_units = FILLER + _pages("单位：元\n单位：万元\n原材料 313,620.68 98.02")
    both2 = ([dr._squash("".join(l)) for l in two_units], [dr._spaced(" ".join(l)) for l in two_units])
    assert dr.amount_before(both2, 6, "98.02") is None                  # 一页两种单位：不猜


def test_verify_fills_amount_for_cost_driver():
    ex = {"page": 7, "text": "原材料占成本 98.02%", "value": "98.02"}
    out = dr.verify([_raw(exposure=ex)], AMOUNT_PAGE)
    assert out[0]["exposure"]["amount"]["value"] == "313,620.68" and out[0]["exposure"]["amount"]["inferred"]


def test_too_few_drivers_asks_again(monkeypatch):
    calls = []

    def fake_ask(sections, **kw):
        calls.append(1)
        return [_raw(name="铜价")] if len(calls) == 1 else [_raw(name=f"因素{i}") for i in range(4)]
    monkeypatch.setattr(dr, "ask_model", fake_ask)
    out = dr.build_from_lines(PAGES, client=None)
    assert len(calls) == 2 and out["proposed"] == 4
