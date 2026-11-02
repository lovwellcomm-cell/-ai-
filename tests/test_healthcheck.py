"""年报小字体检。

夹具是真实年报的原文行（pdfium 拍平后），每条对应一个踩过的格式差异。
红黄绿是给散户看的结论，抽错一个数就可能亮错灯，所以用例都核对到具体数字和页码。
"""
import pytest

from rag import healthcheck as hc
from rag.healthcheck import GREEN, NONE, RED, YELLOW


def _page(text: str) -> list[str]:
    return text.strip("\n").split("\n")


def _pages(*texts: str) -> list[list[str]]:
    return [_page(t) for t in texts]


# ---------------------------------------------------------------- 勾选框与金额

@pytest.mark.parametrize("text,options,want", [
    ("□适用 √不适用", ("不适用", "适用"), "不适用"),          # 上交所：√ 保留
    ("□适用 不适用", ("不适用", "适用"), "不适用"),           # 深交所：√ 丢了
    ("适用 □不适用", ("不适用", "适用"), "适用"),
    ("√ 适用 □ 不适用", ("不适用", "适用"), "适用"),          # 万科：符号和字之间有空格
    ("□是 否", ("是", "否"), "否"),
    ("是 □否", ("是", "否"), "是"),                          # 蓝色光标：重述了
    ("适用 不适用", ("不适用", "适用"), None),                # 两个都没框：看不出来就不猜
    ("□适用", ("不适用", "适用"), None),
])
def test_checked(text, options, want):
    assert hc._checked(text, options) == want


def test_amounts_split_glued_numbers():
    # 七匹狼 2025 第 8 页：上年和前年的政府补助粘在一起
    assert hc._amounts("29,931,597.84 23,902,060.2254,462,291.37本期确认收益的政府补") == [
        29931597.84, 23902060.22, 54462291.37]


def test_amounts_parentheses_negative_and_no_separator():
    assert hc._amounts("(6,038,699,619.35) (5,552,323,580.18)") == [-6038699619.35, -5552323580.18]
    assert hc._amounts("7757.81 5427.05") == [7757.81, 5427.05]      # 没有千分位也不能读成 757.81
    assert hc._amounts("营业外收入 575,661,994.74 2.94%") == [575661994.74]   # 百分比不是金额


# ---------------------------------------------------------------- 审计意见

def test_audit_sse_sentence_on_page_2():
    pages = _pages(
        "封面",
        "三、 容诚会计师事务所（特殊普通合伙）为本公司出具了标准无保留意见的审计报告。\n"
        "四、公司负责人李晓、主管会计工作负责人张军强",
        # 每份审计报告都有的套话，不能当成「非无保留意见」
        "表中的相关披露；如果披露不充分，我们应当发表非无保留意见。我们的结论基于截至审计报告",
    )
    it = hc.check_audit(pages, hc._flat(pages))
    assert (it["status"], it["value"], it["page"]) == (GREEN, "标准无保留意见", 2)


def test_audit_szse_table_line():
    pages = _pages("封面", "内控审计报告意见类型 标准无保留意见",
                   "审计意见类型 标准的无保留意见\n审计报告签署日期 2026 年 04 月 14 日")
    it = hc.check_audit(pages, hc._flat(pages))
    assert (it["status"], it["value"], it["page"]) == (GREEN, "标准的无保留意见", 3)


def test_audit_going_concern_is_red_and_points_to_report():
    # 万科 2025：第 2 页提示 + 第 146 页审计报告里的小标题
    pages = [["封面"],
             _page("董事会、监事会及董事、监事、高级管理人员保证…毕马威华振会计师事务所对本公司出\n"
                   "具了无保留意见（附持续经营重大不确定性段落）的审计报告，本公司董事会")]
    pages += [[f"第{i}页"] for i in range(3, 146)]
    pages.append(_page("三、与持续经营相关的重大不确定性\n我们提醒财务报表使用者关注"))
    it = hc.check_audit(pages, hc._flat(pages))
    assert it["status"] == RED
    assert it["value"] == "无保留意见（附持续经营重大不确定性段落）"
    assert it["page"] == 146


def test_audit_quarterly_header_is_not_an_opinion():
    pages = _pages("(一)审计意见类型\n□适用 √不适用\n(二)财务报表")
    assert hc.check_audit(pages, hc._flat(pages))["status"] == NONE


# ---------------------------------------------------------------- 会计调整

def test_accounting_restated_with_reason():
    # 蓝色光标 2025 第 7 页
    pages = _pages("五、主要会计数据和财务指标\n公司是否需追溯调整或重述以前年度会计数据\n是 □否\n"
                   "追溯调整或重述原因\n其他原因\n2025 年\n2024 年")
    it = hc.check_accounting(hc._flat(pages))
    assert it["status"] == YELLOW
    assert "（原因：其他原因）" in it["summary"]
    assert it["page"] == 1


def test_accounting_policy_change_alone_stays_green():
    # 时代新材 2025 第 56 页：执行新准则的政策变更，没有差错更正
    pages = _pages("五、公司对会计政策、会计估计变更或重大会计差错更正原因和影响的分析说明\n"
                   "（一）公司对会计政策、会计估计变更原因及影响的分析说明\n√适用 □不适用\n"
                   "1、 重要会计政策变更\n（二）公司对重大会计差错更正原因及影响的分析说明\n"
                   "□适用 √不适用\n（三）与前任会计师事务所进行的沟通情况")
    it = hc.check_accounting(hc._flat(pages))
    assert it["status"] == GREEN
    assert "见第 1 页" in it["detail"]


def test_accounting_error_correction_is_red():
    pages = _pages("（二）公司对重大会计差错更正原因及影响的分析说明\n√适用 □不适用")
    assert hc.check_accounting(hc._flat(pages))["status"] == RED


def test_accounting_szse_unrestated():
    pages = _pages("六、主要会计数据和财务指标\n公司是否需追溯调整或重述以前年度会计数据\n□是 否\n2025 年")
    assert hc.check_accounting(hc._flat(pages))["status"] == GREEN


# ---------------------------------------------------------------- 前五名客户 / 供应商

SSE_TOP5 = """
前五名客户销售额1,225,653.66万元，占年度销售总额57.09%；其中前五名客户销售额中关联方销
售额417,858.60万元，占年度销售总额19.46%。
前五名供应商采购额332,971.42万元，占年度采购总额23.63%；其中前五名供应商采购额中关联
方采购额0万元，占年度采购总额0%。
"""

SZSE_TOP5 = """
前五名客户合计销售金额（元） 216,170,690,497.41
前五名客户合计销售金额占年度销售总额比例 65.04%
前五名客户销售额中关联方销售额占年度销售总额比例 0.00%
前五名供应商合计采购金额（元） 132,000,748,088.50
前五名供应商合计采购金额占年度采购总额比例 52.97%
前五名供应商采购额中关联方采购额占年度采购总额比例 0.00%
"""


def test_top5_sse_sentence():
    # 时代新材 2025 第 16 页
    items = {it["key"]: it for it in hc.check_top5(hc._flat(_pages("x", SSE_TOP5)))}
    assert (items["customers"]["status"], items["customers"]["value"]) == (YELLOW, "57.09%")
    assert (items["suppliers"]["status"], items["suppliers"]["value"]) == (GREEN, "23.63%")
    assert (items["related"]["status"], items["related"]["value"]) == (YELLOW, "19.46%")
    assert items["customers"]["page"] == 2


def test_top5_sse_related_without_percent_sign():
    # 精达 2025：「占年度销售总额0 。」没有百分号
    text = ("前五名客户销售额238,841.38万元，占年度销售总额9.69%；其中前五名客户销售额中关联方销售\n"
            "额0万元，占年度销售总额0 。")
    assert hc.top5_ratios(hc._flat(_pages(text)))["cust_rel"] == (0.0, 1)


def test_top5_szse_table():
    # 立讯精密 2025 第 19 页
    items = {it["key"]: it for it in hc.check_top5(hc._flat(_pages(SZSE_TOP5)))}
    assert items["customers"]["value"] == "65.04%"
    assert items["suppliers"]["value"] == "52.97%"
    assert items["related"]["status"] == GREEN


def test_top5_not_disclosed():
    # 万科没有这一节：写「未披露」而不是「未找到」
    items = hc.check_top5(hc._flat(_pages("按欠款方归集的年末余额前五名的应收账款")))
    assert all(it["status"] == NONE and it["value"] == "未披露" for it in items)


# ---------------------------------------------------------------- 现金流 / 利润含金量

def _fin(year="2025", **values):
    return {"years": [year, str(int(year) - 1), str(int(year) - 2)], "page": 7,
            "items": {k: {"values": {year: v}} for k, v in values.items()}}


def test_cash_negative_ocf_with_profit_is_red():
    it = hc.check_cash(_fin(ocf=-6.57e8, np_parent=1.37e8), "FY")
    assert it["status"] == RED


def test_cash_quarterly_capped_at_yellow():
    # 精达 2026 一季报：季度现金流为负很常见，不亮红灯
    it = hc.check_cash(_fin(year="2026", ocf=-6.57e8, np_parent=1.37e8), "Q1")
    assert it["status"] == YELLOW
    assert "以年报为准" in it["summary"]


def test_profit_mostly_nonrecurring_is_red():
    # 七匹狼 2025：归母 3.33 亿，扣非只有 961.53 万
    it = hc.check_profit(_fin(np_parent=332654074.74, np_deducted=9615292.10), [], "FY")
    assert it["status"] == RED
    assert it["value"] == "97.11%"


def test_profit_loss_from_nonrecurring_is_yellow():
    # 七匹狼 2026 三季报：归母亏损，但扣非是赚的
    it = hc.check_profit(_fin(year="2026", np_parent=-50553300.0, np_deducted=81934000.0), [], "Q3")
    assert it["status"] == YELLOW


def test_profit_negative_nonrecurring_is_green():
    # 牧原 2025：扣非高于归母
    it = hc.check_profit(_fin(np_parent=15486891254.04, np_deducted=15988392483.90), [], "FY")
    assert it["status"] == GREEN


def test_gov_subsidy_glued_amounts():
    # 七匹狼 2025 第 8 页（单位：元）
    page = _page("""九、非经常性损益项目及金额
适用 □不适用
单位：元
项目 2025 年金额 2024 年金额 2023 年金额 说明
计入当期损益的政府补助（与公司正常经营业务密切相
关，符合国家政策规定、按照确定的标准享有、对公司
损益产生持续影响的政府补助除外）
29,931,597.84 23,902,060.2254,462,291.37本期确认收益的政府补""")
    assert hc._gov_subsidy([page]) == (29931597.84, 1)


def test_gov_subsidy_in_wan_yuan():
    # 时代新材 2025 第 8 页：这张表是万元
    page = _page("""单位：万元 币种：人民币
非经常性损益项目 2025 年金额 附注（如适用） 2024 年金额 2023 年金额
计入当期损益的政府补助，但与公司正常
经营业务密切相关、符合国家政策规定、
按照确定的标准享有、对公司损益产生持
续影响的政府补助除外
7,757.81 5,427.05 5,787.15""")
    amount, _ = hc._gov_subsidy([page])
    assert amount == pytest.approx(77578100.0)


# ---------------------------------------------------------------- 商誉

def test_goodwill_with_note_reference():
    # 时代新材 2025 第 84 页：科目和金额之间夹着附注号
    pages = _pages("合并资产负债表\n2025 年 12 月 31 日\n单位：元 币种：人民币\n"
                   "无形资产 （七）26 1,000.00 900.00\n"
                   "商誉 （七）27 2,608,450.10 2,608,450.10\n"
                   "长期待摊费用 （七）28 98,877,471.46 127,606,226.62")
    assert hc.goodwill(pages) == (2608450.10, 1, True)


def test_goodwill_blank_row_and_glued_row():
    blank = _pages("1、合并资产负债表\n商誉\n长期待摊费用 135,324,580.08 152,105,949.85")
    assert hc.goodwill(blank) == (0.0, 1, True)
    # 七匹狼 2026 三季报：一整页挤成一行，空的商誉粘上了下一个科目
    glued = _pages("1、合并资产负债表",
                   "无形资产 256,881,875.17 262,891,030.82 其中：数据资源 开发支出 其中：数据资源 "
                   "商誉长期待摊费用 95,351,270.28 152,781,916.90")
    assert hc.goodwill(glued) == (0.0, 2, True)


def test_goodwill_row_absent_counts_as_zero():
    # 恒瑞 2025 第 133 页：资产负债表里干脆没有这一行
    pages = _pages("合并资产负债表\n无形资产 七、26 1,956,487,109.21 1,192,307,211.50\n"
                   "开发支出 八、2 4,877,890,710.83 3,837,558,941.33")
    assert hc.goodwill(pages) == (0.0, 1, False)


def test_goodwill_ignores_toc_and_negative_goodwill():
    pages = _pages("目录\n合并资产负债表 7 - 8",
                   "营业外收入 575,661,994.74 2.94% 主要是新取得子公司产生的负商誉",
                   "合并资产负债表\n商誉 2,239,173,155.52 1,885,826,820.02")
    assert hc.goodwill(pages) == (2239173155.52, 3, True)


def test_goodwill_ratio_uses_balance_sheet_equity_when_financials_missing():
    # 蓝色光标 2025：主要会计数据表抽不到，净资产从资产负债表兜底
    pages = _pages("1、合并资产负债表\n商誉 1,759,925,100.00 1,800,000,000.00",
                   "归属于母公司所有者权益合计 7,892,369,448.23 7,590,329,971.13\n"
                   "少数股东权益 1,844,109.04 -7,512,935.09")
    it = hc.check_goodwill(None, pages)
    assert (it["status"], it["value"]) == (YELLOW, "22.30%")


# ---------------------------------------------------------------- 大股东质押

def test_pledge_szse_unchecked_and_checked():
    green = _pages("4、公司控股股东或第一大股东及其一致行动人累计质押股份数量占其所持公司股份数量比例达到 80%\n"
                   "□适用 不适用\n5、其他持股在 10%以上的法人股东")
    assert hc.check_pledge(hc._flat(green))["status"] == GREEN
    red = _pages("五、公司控股股东或第一大股东及其一致行动人累计质押股份数量占其所持公司股份数量比例\n"
                 "达到 80%以上\n√适用 □不适用")
    assert hc.check_pledge(hc._flat(red))["status"] == RED


# ---------------------------------------------------------------- 汇总

def test_build_quarterly_marks_annual_only_items():
    out = hc.build_from_lines([["空白页"]], _fin(year="2026", ocf=1e8, np_parent=1e8,
                                                np_deducted=0.9e8), "Q3")
    by_key = {it["key"]: it for it in out["items"]}
    assert [it["key"] for it in out["items"]] == list(hc.ORDER)
    for key in hc.ANNUAL_ONLY:
        assert by_key[key]["value"] == "仅年报披露"
    assert by_key["cash"]["status"] == GREEN
    assert sum(out["counts"].values()) == len(hc.ORDER)


def test_every_item_carries_its_public_rule():
    out = hc.build_from_lines([["空白页"]], None, "FY")
    assert all(it["rule"] == hc.RULES[it["key"]] for it in out["items"])


# ---------------------------------------------------------------- 回合与卡片

from ui import memory, payload, turn as turn_core      # noqa: E402


@pytest.mark.parametrize("q,want", [
    ("给精达股份做个年报体检", True),
    ("这家公司有什么风险？", True),
    ("帮我排雷", True),
    ("财务上有没有隐患", True),
    ("年报提示了哪些经营风险？", False),      # 管理层自述的风险：走正常回答
    ("营业收入是多少？", False),
])
def test_wants_health(q, want):
    assert hc.wants_health(q) is want


def test_button_question_is_recognized():
    assert hc.wants_health(hc.button_question("精达股份"))


def _sample():
    return hc.build_from_lines(
        _pages("4、公司控股股东或第一大股东及其一致行动人累计质押股份数量占其所持公司股份数量比例达到 80%\n"
               "√适用 □不适用", SZSE_TOP5),
        _fin(ocf=-6.57e8, np_parent=1.37e8, np_deducted=1.30e8), "FY")


def test_brief_lists_red_and_yellow_with_pages():
    text = hc.brief(_sample(), "示例公司 2025 年报")
    assert text.startswith("按《示例公司 2025 年报》的披露做了")
    assert "**红灯**：现金流含金量" in text
    assert "大股东质押 超过 80%（第 1 页）" in text
    assert "**黄灯**：客户集中度 65.04%（第 2 页）" in text
    assert "不构成投资建议" in text


def test_health_entry_shape_and_rules_restored_in_payload():
    entry = turn_core.health_entry({"q": "体检", "doc_id": "a" * 16}, _sample(), "示例 2025 年报")
    assert entry["kind"] == "health" and entry["hits"] == [] and entry["queries"] == []
    assert all("rule" not in it for it in entry["health"]["items"])      # 历史里不存固定文字
    msg = payload.ai_message(entry)
    by_key = {it["key"]: it for it in msg["health"]["items"]}
    assert by_key["pledge"]["rule"] == hc.RULES["pledge"]
    assert by_key["pledge"]["status"] == "red"


def test_payload_sanitizes_restored_health():
    # 浏览器存档带回来的数据由访客控制：不认识的项丢掉，灯色和页码按白名单
    raw = {"items": [{"key": "audit", "status": "purple", "page": "3", "value": "x" * 99},
                     {"key": "evil", "status": "red"}, "not a dict"]}
    card = payload.health_card(raw)
    assert [it["key"] for it in card["items"]] == ["audit"]
    it = card["items"][0]
    assert it["status"] == "none" and it["page"] is None and len(it["value"]) == 40


def test_health_page_attaches_to_its_own_turn():
    entry = turn_core.health_entry({"q": "体检"}, _sample(), "")
    later = {"q": "营收多少", "a": "答", "hits": [], "queries": []}
    page = {"hp": 2, "turn": 0, "png": "data:image/png;base64,AA"}
    msgs = payload.build_messages([entry, later], None, page=page)
    assert msgs[1]["page"] == page          # 第 0 轮的回答
    assert "page" not in msgs[3]            # 不会挂到最后一条上


def test_memory_keeps_health_kind_and_card():
    entry = turn_core.health_entry({"q": "体检", "doc_id": "a" * 16}, _sample(), "")
    back = memory.clean_entry(memory.pack([entry])[0], {"a" * 16})
    assert back["kind"] == "health"
    assert back["health"]["items"][0]["key"] == "audit"
