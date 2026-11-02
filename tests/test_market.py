"""行情（rag/market.py）。夹具是交易所 / 外汇中心真实返回的片段，网络全部 mock。

这里的数字直接给散户看，算错一个就是误导：主力合约选错、休市日往前找错、小计行混进来，都要挡住。
"""
import json
from datetime import date

import pytest

from rag import config, market

# 上期所 kx20261030.dat 节选：同一品种多个合约 + 「小计」行
SHFE = json.dumps({"o_curinstrument": [
    {"PRODUCTGROUPID": "cu  ", "DELIVERYMONTH": "2611", "SETTLEMENTPRICE": 109010, "OPENINTEREST": 61271},
    {"PRODUCTGROUPID": "cu  ", "DELIVERYMONTH": "2612", "SETTLEMENTPRICE": 108190, "OPENINTEREST": 186825},
    {"PRODUCTGROUPID": "cu  ", "DELIVERYMONTH": "2701", "SETTLEMENTPRICE": 107760, "OPENINTEREST": 126457},
    {"PRODUCTGROUPID": "cu  ", "DELIVERYMONTH": "小计", "SETTLEMENTPRICE": "", "OPENINTEREST": 374553},
    {"PRODUCTGROUPID": "ru", "DELIVERYMONTH": "2701", "SETTLEMENTPRICE": 19205, "OPENINTEREST": 150000},
]}, ensure_ascii=False).encode("utf-8")

# 郑商所 FutureDataDaily.txt 节选：数字带千分位，「小计」行
CZCE = """\t\t\t\t\t郑州商品交易所期货每日行情表(2026-10-30)
合约代码|昨结算    |今开盘    |最高价    |最低价    |今收盘    |今结算    |涨跌1    |涨跌2    |成交量(手)|持仓量    |增减量    |成交额(万元)|交割结算价
AP612 |6,197.00  |6,239.00  |6,613.00  |6,088.00  |6,602.00  |6,379.00  |405.00   |182.00   |13,802    |7,676     |-3,093    |88,042.03   |
AP701 |5,524.00  |5,505.00  |5,888.00  |5,365.00  |5,887.00  |5,678.00  |363.00   |154.00   |609,115   |171,692   |15,152    |3,458,788.37|
小计    |          |          |          |          |          |          |         |         |710,856   |242,840   |9,140     |4,017,533.68|
""".encode("utf-8")

# 广期所 loadList 节选：交割月为空的是小计
GFEX = json.dumps({"code": "0", "data": [
    {"varietyOrder": "lc", "delivMonth": "2611", "clearPrice": 91000, "openInterest": 20000},
    {"varietyOrder": "lc", "delivMonth": "2701", "clearPrice": 90500, "openInterest": 300000},
    {"varietyOrder": "lc", "delivMonth": "", "clearPrice": None, "openInterest": 320000},
]}).encode("utf-8")


def test_parse_shfe_picks_max_open_interest_and_skips_subtotal():
    out = market.parse_shfe(SHFE)
    assert out["cu"] == {"contract": "cu2612", "settle": 108190.0, "oi": 186825.0}
    assert out["ru"]["contract"] == "ru2701"


def test_parse_czce_thousands_separators():
    out = market.parse_czce(CZCE)
    assert out == {"AP": {"contract": "AP701", "settle": 5678.0, "oi": 171692.0}}


def test_parse_gfex():
    assert market.parse_gfex(GFEX)["lc"]["contract"] == "lc2701"


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _fake_shfe(prices: dict, calls: list):
    """prices: {"20261102": 107580, …}；没列出的日子当休市（404）。"""
    def fake_get(url, form=None):
        calls.append(url)
        d = url.rsplit("kx", 1)[1][:8]
        if d not in prices:
            raise market.Closed()
        return json.dumps({"o_curinstrument": [
            {"PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2612",
             "SETTLEMENTPRICE": prices[d], "OPENINTEREST": 1}]}).encode()
    return fake_get


def test_commodity_change_walks_back_over_holidays(tmp_data, monkeypatch):
    # 今天 11-02 有行情；91 天前是 08-03，假设那天和前一天都休市，要退到 08-01
    calls = []
    monkeypatch.setattr(market, "_get", _fake_shfe({"20261102": 107580, "20260801": 105000}, calls))
    q = market.commodity("cu", "shfe", "铜", today=date(2026, 11, 2))
    assert q["latest"] == {"date": "2026-11-02", "contract": "cu2612", "value": 107580.0}
    assert q["base"]["date"] == "2026-08-01"
    assert q["change_pct"] == round((107580 - 105000) / 105000 * 100, 2)
    assert q["unit"] == "元/吨" and q["source"] == "上海期货交易所"


def test_past_days_cached_today_not(tmp_data, monkeypatch):
    calls = []
    monkeypatch.setattr(market, "_get", _fake_shfe({"20261102": 1.0, "20260803": 1.0}, calls))
    market.commodity("cu", "shfe", "铜", today=date(2026, 11, 2))
    n = len(calls)
    market.commodity("cu", "shfe", "铜", today=date(2026, 11, 2))
    # 第二次只重新请求「今天」（收盘前可能还没发布，不缓存）；08-03 走缓存
    assert len(calls) == n + 1 and calls[-1].endswith("kx20261102.dat")


def test_network_error_gives_none_without_caching(tmp_data, monkeypatch):
    def boom(url, form=None):
        raise TimeoutError()
    monkeypatch.setattr(market, "_get", boom)
    assert market.commodity("cu", "shfe", "铜", today=date(2026, 11, 2)) is None
    assert not list((tmp_data / "_market").glob("*.json"))


def test_fx_three_month_change(tmp_data, monkeypatch):
    recs = [{"date": "2026-11-02", "values": ["6.7146"]}, {"date": "2026-08-04", "values": ["6.7800"]},
            {"date": "2026-08-03", "values": ["6.7898"]}, {"date": "2026-07-31", "values": ["6.8000"]}]
    seen = {}

    def fake_get(url, form=None):
        seen["form"] = form
        return json.dumps({"records": recs}).encode()
    monkeypatch.setattr(market, "_get", fake_get)
    q = market.fx("USD", "美元兑人民币中间价", today=date(2026, 11, 2))
    assert seen["form"]["currency"] == "USD/CNY"          # 历史接口只认 POST 表单
    assert q["base"] == {"date": "2026-08-03", "value": 6.7898}
    assert q["change_pct"] == -1.11


def test_lpr_change_in_basis_points(tmp_data, monkeypatch):
    recs = [{"showDateCN": "2026-10-20 9:00", "1Y": "3.00", "5Y": "3.50"},
            {"showDateCN": "2026-07-21 9:00", "1Y": "3.10", "5Y": "3.50"},
            {"showDateCN": "2026-06-20 9:00", "1Y": "3.10", "5Y": "3.60"}]
    monkeypatch.setattr(market, "_get", lambda url, form=None: json.dumps({"records": recs}).encode())
    q = market.lpr("LPR1Y", "1年期LPR", today=date(2026, 11, 2))
    # 10-20 往前 91 天是 07-21：取当天或之前最近一次报价
    assert q["base"]["date"] == "2026-07-21" and q["change_bp"] == -10
    # 5 年期：07-21 那次已经是 3.50（6 月的 3.60 在基准日之前，不该用）
    assert market.lpr("LPR5Y", "5年期以上LPR", today=date(2026, 11, 2))["change_bp"] == 0


def test_quote_routes_and_unknown_source():
    assert market.quote("lh", "生猪", "search") is None
