"""三表速读。

夹具是真实报表的原文行（pdfium 拍平后），每条对应一个踩过的写法；
最后一组是一份能对上勾稽关系的小报表，走完抽取 → 卡片 → 正文 → 回合 → 下发 → 浏览器存档。
"""
import pytest

from rag import statements as st
from rag.statements import BS, CF, IS
from ui import memory, payload, turn as turn_core


def _rows(text: str, page: int = 1) -> list[tuple[int, str]]:
    return [(page, ln) for ln in text.strip("\n").split("\n")]


class _FakeLocator(st._Locator):
    """假的坐标：本期那一列的数右边缘在 100，上期在 200。"""

    def __init__(self, cur: set[str], prev: set[str]):
        super().__init__()
        self.cur, self.prev = cur, prev

    def right_edges(self, page, digits):
        return [100.0] * (digits in self.cur) + [200.0] * (digits in self.prev)


# ---------------------------------------------------------------- 科目名

@pytest.mark.parametrize("raw,want", [
    ("四、净利润（净亏损以“－”号填列）", "净利润"),
    ("加：营业外收入 七、56", "营业外收入"),
    ("其中：营业收入", "营业收入"),
    ("货币资金 (五)1", "货币资金"),
    ("归属于母公司所有者权益（或股东权益）合计", "归属于母公司所有者权益合计"),
    ("1.归属于母公司股东的净利润（净亏损以“-”号填", "归属于母公司股东的净利润"),   # 括号没闭合（折行）
    ("投资活动产生/ (使用) 的现金流量净额", "投资活动产生的现金流量净额"),          # 安克
    ("筹资活动使用的现金流量净额", "筹资活动产生的现金流量净额"),
    ("加：年初现金及现金等价物余额", "期初现金及现金等价物余额"),
    ("一年内到期的非流动负债", "一年内到期的非流动负债"),                          # 开头的「一」不是序号
])
def test_norm_label(raw, want):
    assert st.norm_label(raw) == want


def test_segments_split_glued_rows():
    # 安克 2025 第 116 页：三行粘成一行，空值写「-」
    line = "发行债券收到的现金 1,096,920,000.00 - 收到其他与筹资活动有关的现金 七、58 (3) - - 筹资活动现金流入小计 2,337,525,626.57 1,476,070,613.28"
    segs = st._segments(line)
    assert len(segs) == 3
    assert segs[2].startswith("筹资活动现金流入小计")
    # 精达 2025 三季报：下一行的科目粘在行尾
    assert st._segments("盈余公积 237,877,816.30 237,877,816.30 一般风险准备")[1] == "一般风险准备"


# ---------------------------------------------------------------- 一行 → 两列

def test_parse_two_columns_and_parenthesized_negatives():
    # 万科 2025 第 155 页
    got = st.parse_rows(_rows("""
一、营业总收入 (五)42 233,432,768,960.43 343,176,440,712.96
加：投资收益(损失以“()”填列) (五)47 (5,840,978,800.58) (2,859,267,580.74)
"""), IS)
    assert got["total_rev"] == {"cur": 233432768960.43, "prev": 343176440712.96, "page": 1}
    assert "invest" not in got                       # 不在清单里的科目不取


def test_parse_label_wrapped_over_lines():
    # 中国巨石 2025 第 86、89 页：科目折成两三行，数单独一行
    got = st.parse_rows(_rows("""
归属于母公司所有者权益
（或股东权益）合计
31,094,570,043.74 30,040,848,847.00
少数股东权益 1,455,761,003.73 1,407,273,831.12
所有者权益（或股东权
益）合计
32,550,331,047.47 31,448,122,678.12
负债和所有者权益（或
股东权益）总计
54,627,103,828.07 53,378,083,328.82
"""), BS)
    assert got["equity_parent"]["cur"] == 31094570043.74
    assert got["equity"]["cur"] == 32550331047.47
    assert got["liab_equity"]["prev"] == 53378083328.82


def test_parse_label_tail_on_next_line():
    # 立讯 2025 第 124 页：数在第一行，科目后半截在下一行（中间隔着页眉页码，_rows_from 已去掉）
    got = st.parse_rows(_rows("""
购建固定资产、无形资产和其他长 17,904,337,668.17 12,110,699,704.87
期资产支付的现金
"""), CF)
    assert got["capex"]["cur"] == 17904337668.17
    # 万科 2025 第 158 页：科目在第一行，「净额」和数在第二行
    got = st.parse_rows(_rows("""
经营活动产生的现金流量
净额 (988,124,860.96) 3,799,847,632.35
"""), CF)
    assert got["op_net"]["cur"] == -988124860.96


def test_page_header_between_wrapped_label_is_skipped():
    pages = [["合并现金流量表", "单位：元", "购建固定资产、无形资产和其他长 1.00 2.00"],
             ["立讯精密工业股份有限公司 2025 年年度报告全文", "125", "项目 附注 2025 年度 2024 年度", "期资产支付的现金"]]
    rows = st.find_statements(pages)[CF]["rows"]
    assert [ln for _, ln in rows][-2:] == ["购建固定资产、无形资产和其他长 1.00 2.00", "期资产支付的现金"]
    assert st.parse_rows(rows, CF)["capex"]["prev"] == 2.0


def test_parse_two_columns_on_two_lines():
    # 精达 2025 三季报第 9 页：本期、上期两个数各占一行
    got = st.parse_rows(_rows("""
1.归属于母公司股东的净利润（净亏损以“-”号填
列）
450,880,616.26
415,852,153.89
2.少数股东损益（净亏损以“-”号填列） 20,222,554.72 11,256,439.35
"""), IS)
    assert got["np_parent"]["cur"] == 450880616.26 and got["np_parent"]["prev"] == 415852153.89
    assert got["minority"]["prev"] == 11256439.35


def test_parse_blank_items_glued_before():
    # 七匹狼 2026 三季报：整页拍成一行，空着的科目挤在有数的科目前面
    got = st.parse_rows(_rows("向中央银行借款 拆入资金 交易性金融负债 衍生金融负债 应付票据 248,627,225.17 395,354,463.20"), BS)
    assert got["notes_pay"]["cur"] == 248627225.17


def test_single_value_column_from_position():
    # 精达 2025 第 71 页「交易性金融资产 2,575,806.90」只有一列有数：按横坐标归到本期；
    # 第 73 页「应付债券 264,773,986.12」在上期那一列
    rows = _rows("""
货币资金 2,069,148,661.10 1,117,554,428.04
交易性金融资产 2,575,806.90
应付债券 264,773,986.12
""")
    loc = _FakeLocator(cur={"2,069,148,661.10", "2,575,806.90"}, prev={"1,117,554,428.04", "264,773,986.12"})
    got = st.parse_rows(rows, BS, loc)
    assert got["trading"] == {"cur": 2575806.9, "prev": None, "page": 1}
    assert got["bonds"] == {"cur": None, "prev": 264773986.12, "page": 1}


def test_single_value_without_position_is_dropped():
    # 夹具没有 PDF：分不清是哪一列就不取，不猜
    got = st.parse_rows(_rows("货币资金 1.00 2.00\n交易性金融资产 2,575,806.90"), BS)
    assert "trading" not in got


# ---------------------------------------------------------------- 找表、单位、勾稽

SMALL = [
    """
二、财务报表
合并资产负债表
2025 年 12 月 31 日
单位：亿元 币种：人民币
项目 附注 2025 年 12 月 31 日 2024 年 12 月 31 日
货币资金 七、1 100.00 80.00
应收账款 50.00 40.00
存货 30.00 30.00
资产总计 180.00 150.00
短期借款 40.00 50.00
应付账款 20.00 10.00
负债合计 60.00 60.00
归属于母公司所有者权益（或股东权益）合计 110.00 85.00
所有者权益（或股东权益）合计 120.00 90.00
负债和所有者权益（或股东权益）总计 180.00 150.00
公司负责人：张三 主管会计工作负责人：李四
母公司资产负债表
货币资金 1.00 1.00
""",
    """
示例股份有限公司 2025 年年度报告
12
合并利润表
单位：亿元 币种：人民币
一、营业总收入 200.00 150.00
其中：营业收入 200.00 150.00
二、营业总成本 165.00 132.00
其中：营业成本 150.00 120.00
销售费用 10.00 8.00
管理费用 5.00 4.00
三、营业利润（亏损以“－”号填列） 35.00 18.00
四、利润总额（亏损总额以“－”号填列） 35.00 18.00
减：所得税费用 5.00 3.00
五、净利润（净亏损以“－”号填列） 30.00 15.00
1.归属于母公司股东的净利润（净亏损以“-”号填列） 28.00 14.00
2.少数股东损益（净亏损以“-”号填列） 2.00 1.00
公司负责人：张三
合并现金流量表
单位：亿元 币种：人民币
经营活动产生的现金流量净额 40.00 20.00
投资活动产生的现金流量净额 (15.00) (10.00)
筹资活动产生的现金流量净额 (5.00) (3.00)
五、现金及现金等价物净增加额 20.00 7.00
加：期初现金及现金等价物余额 80.00 73.00
六、期末现金及现金等价物余额 100.00 80.00
公司负责人：张三
""",
]


def _small(period="FY"):
    return st.build_from_lines([p.strip("\n").split("\n") for p in SMALL], period)


def test_build_small_report_all_checks_pass():
    out = _small()
    assert all(out[k]["ok"] for k in (BS, IS, CF))
    assert out[BS]["page"] == 1 and out[IS]["page"] == 2 and out[CF]["page"] == 2
    assert out[BS]["unit"] == "亿元"
    assert out[BS]["items"]["cash"] == {"cur": 100e8, "prev": 80e8, "page": 1}
    assert "母公司" not in str(out[BS]["items"])                     # 停在签章行，没读进母公司报表
    assert out[CF]["items"]["inv_net"]["cur"] == -15e8
    descs = [c["desc"] for c in out[CF]["checks"]]
    assert "经营 + 投资 + 筹资 + 汇率影响 = 现金净增加额" in descs
    assert "期初现金 + 净增加额 = 期末现金" in descs


def test_unbalanced_statement_is_not_shown():
    pages = [p.strip("\n").split("\n") for p in SMALL]
    pages[0] = [ln.replace("资产总计 180.00", "资产总计 181.00") for ln in pages[0]]
    out = st.build_from_lines(pages)
    assert out[BS]["ok"] is False and out[BS]["why"] == "勾稽关系没对上"
    c = st.card(out, "FY", "2025")
    assert [s["key"] for s in c["sections"]] == [IS, CF]
    assert c["skipped"] == [{"key": BS, "name": "资产负债表", "why": "勾稽关系没对上"}]
    assert "**资产负债表**没有展示：勾稽关系没对上" in st.brief(c, "示例 2025 年报")


def test_missing_required_row():
    pages = [p.strip("\n").split("\n") for p in SMALL]
    pages[1] = [ln for ln in pages[1] if "所得税费用" not in ln]
    out = st.build_from_lines(pages)
    assert out[IS]["ok"] is False and out[IS]["why"] == "缺少所得税费用"


def test_szse_quarter_title():
    # 七匹狼 2026 三季报：「2、合并年初到报告期末利润表」
    found = st.find_statements([["2、合并年初到报告期末利润表", "单位：元", "营业收入 1.00 2.00"]])
    assert IS in found and found[IS]["unit"] == "元"


def test_unit_on_its_own_line():
    # 万科：单位单独一行「人民币元」
    found = st.find_statements([["合并资产负债表", "人民币元", "项目 附注 本年年末余额 上年年末余额"]])
    assert found[BS]["unit"] == "元"


def test_toc_line_is_not_a_title():
    assert st.find_statements([["合并资产负债表 7 - 8"]]) == {}


def test_financial_company_not_supported():
    found = [["合并利润表", "单位：元", "一、营业总收入 10.00 9.00", "利息净收入 8.00 7.00",
              "四、利润总额 1.00 1.00", "减：所得税费用 0.20 0.20", "五、净利润 0.80 0.80", "公司负责人：甲"]]
    out = st.build_from_lines(found)
    assert out[IS]["ok"] is False and out[IS].get("financial") is True


# ---------------------------------------------------------------- 卡片与正文

def test_col_labels():
    assert st.col_labels("FY", "2025") == (("2025 年末", "2024 年末"), ("2025 年", "2024 年"))
    assert st.col_labels("Q3", 2026) == (("2026 年 9 月末", "2025 年末"), ("2026 年前三季度", "2025 年前三季度"))
    assert st.col_labels("H1", "") == (("期末", "上年末"), ("本期", "上年同期"))


def test_card_numbers():
    c = st.card(_small(), "FY", "2025")
    assert c["unit"] == "亿元" and [s["key"] for s in c["sections"]] == [BS, IS, CF]
    bs, is_, cf = c["sections"]
    rows = {r["label"]: r for r in bs["rows"]}
    assert rows["总资产"]["cur"] == "180.00" and rows["总资产"]["chg"] == "+20.0%"
    assert rows["资产负债率"]["cur"] == "33.3%" and rows["资产负债率"]["chg"] == "-6.7 个百分点"
    assert rows["有息负债"]["cur"] == "40.00" and rows["有息负债"]["hint"] == st.DEBT_NOTE
    assert rows["应收"]["page"] == 1
    assert "账上货币资金 100.00 亿元，有息负债 40.00 亿元，现金比借款多 60.00 亿元" in bs["lead"]
    assert "上下游欠它的（应收 50.00 亿元）比它欠上下游的（应付和预收 20.00 亿元）多" in bs["lead"]
    # 每 100 元：成本 75、销售 5、管理 2.5、所得税 2.5，剩 15（没有其他收支）
    flow = {x["label"]: x["v"] for x in is_["flow"]}
    assert flow == {"营业成本": 75.0, "销售费用": 5.0, "管理费用": 2.5, "所得税": 2.5, "净利润": 15.0}
    assert "最后剩下净利润 15.0 元（2024 年为 10.0 元）" in is_["lead"]
    assert "比 2024 年增长 33.3%" in is_["lead"]
    assert cf["portrait"] == st.PORTRAITS["+--"]
    rows = {r["label"]: r for r in cf["rows"]}
    assert rows["筹资活动净额"]["cur"] == "-5.00" and rows["筹资活动净额"]["chg"] == "-2.00"   # 上期为负：写增减额
    assert rows["资本开支"]["cur"] == "—"


def test_per_hundred_other_and_loss():
    t = st._Table({"items": {"revenue": {"cur": 100.0}, "cost": {"cur": 90.0}, "net_profit": {"cur": -40.0},
                             "credit_imp": {"cur": -20.0}, "op_profit": {"cur": -30.0, "page": 3}}, "page": 1})
    flow = st.per_hundred(t)
    by = {x["label"]: x["v"] for x in flow}
    # 100 − 90 − 20（减值）= −10，净利润 −40：其他收支倒算出 30 元的净支出
    assert by == {"营业成本": 90.0, "减值损失": 20.0, "其他收支": 30.0, "净利润": -40.0}
    assert sum(v for k, v in by.items() if k != "净利润") == pytest.approx(100 - by["净利润"])
    assert st._left(-40.0) == "亏损 40.0 元"


def test_lead_mentions_large_other_income():
    # 七匹狼 2025：成本费用加起来超过 100 元，净利润靠投资收益补回来
    items = {"revenue": {"cur": 100.0, "prev": 100.0}, "cost": {"cur": 46.0, "prev": 46.0},
             "selling": {"cur": 47.0, "prev": 47.0}, "total_profit": {"cur": 13.0, "prev": 13.0},
             "income_tax": {"cur": 2.0, "prev": 2.0}, "net_profit": {"cur": 11.0, "prev": 11.0}}
    sec = st._is_section(st._Table({"items": items, "page": 1}), ("2025 年", "2024 年"), 1.0)
    assert "投资收益、补助等其他收支补回 6.0 元，最后剩下净利润 11.0 元" in sec["lead"]


def test_brief():
    text = st.brief(st.card(_small("Q3"), "Q3", "2026"), "示例 2026 三季报")
    assert text.startswith("按《示例 2026 三季报》里的合并报表")
    assert "**资产负债表**（第 1 页）" in text and "**现金流量表**（第 2 页）" in text
    assert "条勾稽关系" in text and "未经审计" in text
    assert "不构成投资建议" in text and "财报体检" in text


def test_brief_when_nothing_found():
    c = st.card({"bs": {"ok": False, "why": "没找到这张表"}}, "FY", "2025")
    assert "没能从《X》里读出" in st.brief(c, "X")


@pytest.mark.parametrize("q,want", [
    ("三表速读：精达股份的资产负债表、利润表、现金流量表各说明了什么", True),
    ("帮我看懂这份财务报表", True),
    ("三大报表怎么样", True),
    ("三张表说明了什么", True),
    ("资产负债表里货币资金有多少", False),        # 问具体科目走正常回答
    ("现金流量表的经营现金流是多少", False),
])
def test_wants_statements(q, want):
    assert st.wants_statements(q) is want


def test_button_question_is_recognized():
    assert st.wants_statements(st.button_question("精达股份"))


# ---------------------------------------------------------------- 回合、下发、存档

def test_entry_payload_memory_round_trip():
    c = st.card(_small(), "FY", "2025")
    entry = turn_core.statements_entry({"q": "三表速读", "doc_id": "a" * 16}, c, "示例 2025 年报")
    assert entry["kind"] == "statements" and entry["hits"] == [] and entry["queries"] == []
    assert entry["a"].startswith("按《示例 2025 年报》")
    msg = payload.ai_message(entry)
    assert [s["key"] for s in msg["statements"]["sections"]] == [BS, IS, CF]
    back = memory.clean_entry(memory.pack([entry])[0], {"a" * 16})
    assert back["kind"] == "statements"
    assert payload.statements_card(back["statements"]) == msg["statements"]


def test_payload_sanitizes_restored_statements():
    raw = {"unit": "亿元" * 9, "sections": [
        {"key": "evil", "rows": []},
        {"key": "bs", "title": "x" * 99, "page": "3", "rows": [{"label": "总资产", "cur": 1, "page": True}, "bad"],
         "flow": [{"label": "成本", "v": "9"}], "checks": ["a"] * 20},
    ]}
    c = payload.statements_card(raw)
    assert [s["key"] for s in c["sections"]] == ["bs"]
    s = c["sections"][0]
    assert len(s["title"]) == 30 and s["page"] is None
    assert s["rows"] == [{"label": "总资产", "cur": "1", "prev": "", "chg": "", "hint": "", "page": None}]
    assert s["flow"] == [] and len(s["checks"]) == 6 and len(c["unit"]) == 4


def test_page_flip_uses_entry_doc():
    entry = {"kind": "statements", "doc_id": "b" * 16, "statements": {}}
    assert turn_core.health_page_doc(entry, "st:bs", "c" * 16) == "b" * 16


# ---------------------------------------------------------------- 第二步：三表片段进回答

def test_as_hits_shape():
    hits = st.as_hits(_small(), "a" * 16, "示例 2025 年报", "FY", "2025")
    assert [h["section"] for h in hits] == ["三表数据 · 资产负债表", "三表数据 · 利润表",
                                            "三表数据 · 现金流量表", "三表数据 · 程序计算的指标"]
    bs = hits[0]
    assert bs["doc_id"] == "a" * 16 and bs["page_start"] == 1 and not bs.get("derived")
    assert "货币资金：2025 年末 100.00 亿元；2024 年末 80.00 亿元；增减 +25.0%" in bs["text"]
    cf = hits[2]["text"]
    assert "投资活动产生的现金流量净额：2025 年 -15.00 亿元；2024 年 -10.00 亿元" in cf   # 上期为负不写百分比
    calc = hits[3]
    assert calc["derived"] is True and calc["text"].startswith("以下指标由程序")
    # 片段里的比率两位小数（年报披露的毛利率就是两位，片段更粗的话回答会引用粗的那个）
    assert "资产负债率（负债合计 ÷ 资产总计）：2025 年末 33.33%；2024 年末 40.00%；变化 -6.67 个百分点" in calc["text"]
    assert "有息负债（" + st.DEBT_NOTE + "）：2025 年末 40.00 亿元" in calc["text"]
    assert "每 100 元营业收入的去向（2025 年" in calc["text"] and "剩下净利润 15.0 元" in calc["text"]
    assert "现金流画像（2025 年" in calc["text"]


def test_as_hits_skips_unreconciled_table():
    pages = [p.strip("\n").split("\n") for p in SMALL]
    pages[0] = [ln.replace("资产总计 180.00", "资产总计 181.00") for ln in pages[0]]
    hits = st.as_hits(st.build_from_lines(pages), "a" * 16, "x", "FY", "2025")
    assert all("资产负债表" not in h["section"] for h in hits)
    assert "资产负债率" not in hits[-1]["text"] and "毛利率" in hits[-1]["text"]
    assert st.as_hits(None, "a" * 16, "x") == [] and st.as_hits({"bs": {"ok": False}}, "a" * 16, "x") == []


def test_quarter_hit_columns():
    hits = st.as_hits(_small("Q3"), "a" * 16, "示例 2026 三季报", "Q3", "2026")
    assert "货币资金：2026 年 9 月末 100.00 亿元；2025 年末 80.00 亿元" in hits[0]["text"]
    assert "营业收入：2026 年前三季度 200.00 亿元；2025 年前三季度 150.00 亿元" in hits[1]["text"]


@pytest.mark.parametrize("q,last,want", [
    ("有息负债有多少？", None, True),
    ("资产负债率比去年变化多少", None, True),
    ("自由现金流是多少", None, True),
    ("前五大客户占比多少", None, False),
    ("研发人员有多少", None, False),
    ("为什么增加这么多？", "statements", True),      # 三表速读之后的追问
    ("为什么增加这么多？", "health", False),
])
def test_wants_items(q, last, want):
    assert st.wants_items(q, last) is want


def test_attach_replaces_old_statement_hits():
    old = st.as_hits(_small(), "a" * 16, "x", "FY", "2025")
    base = [{"section": "第三节", "text": "原文"}]
    once = st.attach(base, old)
    again = st.attach(once, old)
    assert len(again) == len(once) == 1 + len(old)
    assert st.attach(base, []) == base


def test_generator_rule_only_with_statement_hits():
    from rag.generator import STMT_RULE, build_messages

    hits = st.as_hits(_small(), "a" * 16, "x", "FY", "2025")
    with_rule = build_messages("有息负债多少", hits)[0]["content"]
    plain = build_messages("有息负债多少", [{"section": "第三节", "text": "x", "page_start": 1, "page_end": 1}])[0]["content"]
    assert STMT_RULE in with_rule and STMT_RULE not in plain


def test_derived_hit_not_recorded_as_note():
    from rag.notes import from_answer

    hits = st.as_hits(_small(), "a" * 16, "示例 2025 年报", "FY", "2025")
    n = len(hits)
    answer = ("| 指标 | 2025 年末 |\n|---|---|\n"
              f"| 货币资金 | 100.00 亿元 [1] |\n| 有息负债 | 40.00 亿元 [{n}] |\n")
    notes = from_answer(answer, hits, [], "a" * 16, "示例 2025 年报")
    assert [x["t"].split("：")[0] for x in notes] == ["货币资金"]      # 程序算的有息负债不记成笔记
