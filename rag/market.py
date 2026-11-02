"""行情：用交易所和外汇交易中心公开发布的数据，算一个因素最近 3 个月变了多少。

数据来源（rag/netcheck.MARKET，2026-11-02 线上实测都能访问）：
  · 上期所日行情 JSON、郑商所日行情文本、广期所日行情 JSON —— 每个品种取持仓量最大的合约
    当「主力合约」，比较最近一个交易日和约 3 个月前那个交易日的结算价
  · 外汇交易中心：人民币汇率中间价历史（POST）、LPR 历史
大商所的公开页面有访问校验，不在这里（也不去绕开）：它的品种由 rag/drivers 走联网搜索。

所有数字都是官方原始数据经程序计算，不经过模型。拿不到就返回 None，界面写「暂时取不到」，不猜。
历史某一天的行情不会再变，按「交易所 + 日期」缓存在 data/_market/；当天的中间价 / LPR 按天缓存。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
TZ = timezone(timedelta(hours=8))
LOOKBACK_DAYS = 91          # 「近 3 个月」
MAX_BACK = 10               # 往前找最近交易日最多找几天（国庆长假 8 天）
TIMEOUT = 15

EXCHANGES = {
    "shfe": "上海期货交易所", "czce": "郑州商品交易所", "gfex": "广州期货交易所",
    "ccpr": "中国外汇交易中心", "lpr": "中国外汇交易中心",
}
# 期货结算价的单位：除了黄金、白银都是元/吨
UNITS = {"au": "元/克", "ag": "元/千克"}
CCPR_PAIRS = {"USD": "USD/CNY", "EUR": "EUR/CNY", "HKD": "HKD/CNY", "JPY": "100JPY/CNY"}

SHFE_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{d}.dat"
CZCE_URL = "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{y}/{d}/FutureDataDaily.txt"
# 广期所的 https 证书主机名不匹配，只能走 http；数据是公开行情，不带任何凭据
GFEX_URL = "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
CCPR_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
LPR_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis"


class Closed(Exception):
    """那天没有行情（休市、或当天还没发布）。"""


def beijing_today() -> date:
    """部署在海外（UTC），「今天」要按北京时间算。"""
    return datetime.now(TZ).date()


def _get(url: str, form: dict | None = None) -> bytes:
    body = urllib.parse.urlencode(form).encode("utf-8") if form else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Closed() from e
        raise


def _num(raw) -> float | None:
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _keep_main(out: dict, product: str, contract: str, settle, oi) -> None:
    """同一品种只留持仓量最大的合约（主力合约）。"""
    s, o = _num(settle), _num(oi)
    if not product or not s or s <= 0 or o is None:
        return
    if product not in out or o > out[product]["oi"]:
        out[product] = {"contract": contract, "settle": s, "oi": o}


# ---------------------------------------------------------------- 三个交易所的格式

def parse_shfe(raw: bytes) -> dict:
    """{"o_curinstrument": [{"PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2612", "SETTLEMENTPRICE": …,
    "OPENINTEREST": …}, …]}。交割月不是数字的是小计行。"""
    out: dict = {}
    for r in json.loads(raw.decode("utf-8")).get("o_curinstrument") or []:
        month = str(r.get("DELIVERYMONTH") or "").strip()
        product = str(r.get("PRODUCTGROUPID") or "").strip()
        if month.isdigit():
            _keep_main(out, product, product + month, r.get("SETTLEMENTPRICE"), r.get("OPENINTEREST"))
    return out


_CZCE_CODE_RE = re.compile(r"^([A-Z]{1,3})(\d{3})$")


def parse_czce(raw: bytes) -> dict:
    """竖线分隔：合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量|持仓量|…
    数字带千分位；「小计」「总计」行的代码不是「字母 + 3 位数字」，跳过。"""
    out: dict = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        cells = [c.strip() for c in line.split("|")]
        m = _CZCE_CODE_RE.match(cells[0]) if len(cells) > 10 else None
        if m:
            _keep_main(out, m.group(1), cells[0], cells[6], cells[10])
    return out


def parse_gfex(raw: bytes) -> dict:
    """{"data": [{"varietyOrder": "lc", "delivMonth": "2611", "clearPrice": …, "openInterest": …}]}"""
    out: dict = {}
    for r in json.loads(raw.decode("utf-8")).get("data") or []:
        month = str(r.get("delivMonth") or "").strip()
        product = str(r.get("varietyOrder") or "").strip()
        if month.isdigit():
            _keep_main(out, product, product + month, r.get("clearPrice"), r.get("openInterest"))
    return out


def _fetch_day(exchange: str, day: date) -> dict:
    d = day.strftime("%Y%m%d")
    if exchange == "shfe":
        parsed = parse_shfe(_get(SHFE_URL.format(d=d)))
    elif exchange == "czce":
        parsed = parse_czce(_get(CZCE_URL.format(y=day.year, d=d)))
    elif exchange == "gfex":
        parsed = parse_gfex(_get(GFEX_URL, {"trade_date": d, "trade_type": "0"}))
    else:
        raise ValueError(exchange)
    if not parsed:
        raise Closed()
    return parsed


def _cache_dir():
    path = config.DATA_DIR / "_market"
    path.mkdir(parents=True, exist_ok=True)
    return path


def daily(exchange: str, day: date, today: date | None = None) -> dict | None:
    """某交易所某天各品种的主力合约 {品种: {"contract", "settle", "oi"}}。
    休市返回 {}；网络出错返回 None（不缓存，下次再试）。
    过去的日子（含休市）写缓存；今天的不写 —— 收盘前还没发布，晚点再问可能就有了。"""
    today = today or beijing_today()
    path = _cache_dir() / f"{exchange}_{day:%Y%m%d}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        parsed = _fetch_day(exchange, day)
    except Closed:
        parsed = {}
    except Exception:
        return None
    if day < today:
        try:
            path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return parsed


def settle_on_or_before(exchange: str, product: str, day: date,
                        today: date | None = None) -> dict | None:
    """day 当天或之前最近一个有这个品种行情的交易日：{"date", "contract", "value"}。"""
    for back in range(MAX_BACK + 1):
        d = day - timedelta(days=back)
        rows = daily(exchange, d, today)
        if rows is None:
            return None                              # 网络出错：别往前乱找，直接放弃
        if product in rows:
            r = rows[product]
            return {"date": d.isoformat(), "contract": r["contract"], "value": r["settle"]}
    return None


def _pct(new: float, old: float) -> float | None:
    return round((new - old) / abs(old) * 100, 2) if old else None


def commodity(code: str, exchange: str, name: str, today: date | None = None) -> dict | None:
    today = today or beijing_today()
    latest = settle_on_or_before(exchange, code, today, today)
    if not latest:
        return None
    base_day = date.fromisoformat(latest["date"]) - timedelta(days=LOOKBACK_DAYS)
    base = settle_on_or_before(exchange, code, base_day, today)
    if not base:
        return None
    return {"code": code, "name": name, "source": EXCHANGES[exchange],
            "basis": "主力合约结算价", "unit": UNITS.get(code, "元/吨"),
            "latest": latest, "base": base, "change_pct": _pct(latest["value"], base["value"])}


# ---------------------------------------------------------------- 外汇中心：中间价、LPR

def _day_cached(name: str, today: date, fetch) -> dict | None:
    path = _cache_dir() / f"{name}_{today:%Y%m%d}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        data = fetch()
    except Exception:
        return None
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def _nearest_before(series: list[tuple[str, float]], day: date) -> tuple[str, float] | None:
    """series 按日期倒序。取 day 当天或之前最近的一条。"""
    for d, v in series:
        if date.fromisoformat(d) <= day:
            return d, v
    return None


def fx(code: str, name: str, today: date | None = None) -> dict | None:
    pair = CCPR_PAIRS.get(code)
    if not pair:
        return None
    today = today or beijing_today()
    start = today - timedelta(days=LOOKBACK_DAYS + MAX_BACK + 5)

    def fetch():
        raw = _get(CCPR_URL, {"startDate": start.isoformat(), "endDate": today.isoformat(),
                              "currency": pair, "pageNum": "1", "pageSize": "200"})
        recs = json.loads(raw.decode("utf-8")).get("records") or []
        return [[r["date"], _num((r.get("values") or [None])[0])] for r in recs
                if r.get("date") and _num((r.get("values") or [None])[0])]

    series = _day_cached(f"ccpr_{code}", today, fetch)
    if not series:
        return None
    series = sorted(((d, v) for d, v in series), reverse=True)
    latest = series[0]
    base = _nearest_before(series, date.fromisoformat(latest[0]) - timedelta(days=LOOKBACK_DAYS))
    if not base:
        return None
    return {"code": code, "name": name, "source": EXCHANGES["ccpr"], "basis": "人民币汇率中间价",
            "unit": "人民币元" if code != "JPY" else "人民币元/100日元",
            "latest": {"date": latest[0], "value": latest[1]},
            "base": {"date": base[0], "value": base[1]},
            "change_pct": _pct(latest[1], base[1])}


def lpr(code: str, name: str, today: date | None = None) -> dict | None:
    term = {"LPR1Y": "1Y", "LPR5Y": "5Y"}.get(code)
    if not term:
        return None
    today = today or beijing_today()
    start = today - timedelta(days=LOOKBACK_DAYS + 45)

    def fetch():
        raw = _get(LPR_URL + "?" + urllib.parse.urlencode(
            {"lang": "CN", "strStartDate": start.isoformat(), "strEndDate": today.isoformat()}))
        recs = json.loads(raw.decode("utf-8")).get("records") or []
        return [[r["showDateCN"][:10], {"1Y": _num(r.get("1Y")), "5Y": _num(r.get("5Y"))}]
                for r in recs if r.get("showDateCN")]

    data = _day_cached("lpr", today, fetch)
    if not data:
        return None
    series = sorted(((d, v[term]) for d, v in data if v.get(term) is not None), reverse=True)
    if not series:
        return None
    latest = series[0]
    base = _nearest_before(series, date.fromisoformat(latest[0]) - timedelta(days=LOOKBACK_DAYS))
    if not base:
        return None
    return {"code": code, "name": name, "source": EXCHANGES["lpr"], "basis": "贷款市场报价利率",
            "unit": "%", "latest": {"date": latest[0], "value": latest[1]},
            "base": {"date": base[0], "value": base[1]},
            "change_bp": round((latest[1] - base[1]) * 100)}


def quote(code: str, name: str, source: str, today: date | None = None) -> dict | None:
    """rag/drivers.MARKETS 里的一个代码 → 近 3 个月的变化。拿不到返回 None。"""
    if source in ("shfe", "czce", "gfex"):
        return commodity(code, source, name, today)
    if source == "ccpr":
        return fx(code, name, today)
    if source == "lpr":
        return lpr(code, name, today)
    return None
