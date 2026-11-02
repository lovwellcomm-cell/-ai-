"""影响这家公司的几件大事：从年报里找出最能左右业绩的 3–5 个外部因素。

分工（和体检卡一样，数字不交给模型）：
  1. 程序定位原文段落：风险章节、分行业/分产品/分地区收入、成本构成与主要原材料、
     附注里的汇率风险与利率风险。每段带页码，总字数有上限。
  2. 模型从这些段落里挑因素：类型、名称、怎么影响公司、原文引用 + 页码、暴露度数字，
     能对上行情品种的标上品种代码。
  3. 程序逐条核对：引用必须在那一页原文里找得到，暴露度的数字也必须在那一页出现；
     对不上的引用 / 数字删掉，一条引用都核对不上的因素整条不要。

真实年报里的写法差异（13 份样本，tools/eval_drivers.py）：
  · 风险章节：「(四)可能面对的风险」「（三）公司可能面临的风险」「3、公司可能面临的风险」；
    牧原写「详见第一节“重要风险提示”」—— 要跟到前几页去取
  · 分地区：国内/国外、国内地区/国外地区、境内/境外、内销/外销、中国大陆/海外；七匹狼按华南、华东分
  · 成本构成：上交所「成本分析表」里的直接材料占比；深交所「营业成本构成」里写「原材料」
  · 汇率 / 利率：在财务附注「金融工具的风险」里，恒瑞有「人民币对美元贬值 5%」的敏感性表
"""
from __future__ import annotations

import json
import re

from . import config
from .periods import FY

DRIVERS_VERSION = 1
DRIVERS_FILE = "drivers.json"
MAX_DRIVERS = 5

# 「其他」装不进前七类但确实重大的外部因素：长江电力的来水、牧原的疫病
TYPES = ("成本", "售价", "汇率", "利率", "需求", "政策", "客户", "其他")

# 行情品种：代码 → (名称, 数据来源)。来源是交易所 / 官方机构公开发布的日行情（rag/netcheck.MARKET 测过线上可达）。
# 大商所的公开页面有访问校验，不去绕开：它的品种标 "search"，走联网搜索并标「未经核验」。
MARKETS = {
    # 上海期货交易所
    "cu": ("铜", "shfe"), "al": ("铝", "shfe"), "zn": ("锌", "shfe"), "pb": ("铅", "shfe"),
    "ni": ("镍", "shfe"), "sn": ("锡", "shfe"), "ao": ("氧化铝", "shfe"), "au": ("黄金", "shfe"),
    "ag": ("白银", "shfe"), "rb": ("螺纹钢", "shfe"), "hc": ("热轧卷板", "shfe"), "ss": ("不锈钢", "shfe"),
    "ru": ("天然橡胶", "shfe"), "bu": ("石油沥青", "shfe"), "fu": ("燃料油", "shfe"), "sp": ("纸浆", "shfe"),
    # 郑州商品交易所
    "FG": ("玻璃", "czce"), "SA": ("纯碱", "czce"), "TA": ("PTA", "czce"), "MA": ("甲醇", "czce"),
    "CF": ("棉花", "czce"), "SR": ("白糖", "czce"), "UR": ("尿素", "czce"), "PF": ("短纤", "czce"),
    "SF": ("硅铁", "czce"), "SM": ("锰硅", "czce"), "AP": ("苹果", "czce"),
    # 广州期货交易所
    "lc": ("碳酸锂", "gfex"), "si": ("工业硅", "gfex"), "ps": ("多晶硅", "gfex"),
    # 大连商品交易所：没有可用的官方数据接口
    "lh": ("生猪", "search"), "v": ("PVC", "search"), "i": ("铁矿石", "search"), "m": ("豆粕", "search"),
    "c": ("玉米", "search"), "pp": ("聚丙烯", "search"), "l": ("聚乙烯", "search"),
    "eg": ("乙二醇", "search"), "j": ("焦炭", "search"), "jm": ("焦煤", "search"),
    "p": ("棕榈油", "search"), "y": ("豆油", "search"),
    # 中国外汇交易中心
    "USD": ("美元兑人民币中间价", "ccpr"), "EUR": ("欧元兑人民币中间价", "ccpr"),
    "HKD": ("港元兑人民币中间价", "ccpr"), "JPY": ("100日元兑人民币中间价", "ccpr"),
    "LPR1Y": ("1年期LPR", "lpr"), "LPR5Y": ("5年期以上LPR", "lpr"),
}

# ---------------------------------------------------------------- 定位原文段落

_RISK_RE = re.compile(r"^[(（]?[一二三四五六七八九十\d]{1,2}[)）、.]?\s*(?:公司)?(?:可能)?(?:面对|面临)的(?:主要)?风险"
                      r"|可能产生影响的风险因素")
# 没有单列风险章节时退而求其次（立讯只有「十一、公司未来发展的展望」，万科是「特别风险提示」）
_RISK_FALLBACK_RE = re.compile(r"^[(（]?[一二三四五六七八九十\d]{0,3}[)）、.]?\s*(?:公司)?未来发展的展望|^特别风险提示")
_RISK_REF_RE = re.compile(r"详见.{0,12}重要风险提示|重要风险提示")
_MIX_RE = re.compile(r"主营业务分行业|营业收入构成|主营业务经营情况")
_COST_RE = re.compile(r"成本分析表|营业成本构成|成本构成项目")
_MATERIAL_RE = re.compile(r"主要原材料的基本情况")
# 前五名客户：立讯前五名客户占 65%，不给这一页模型就看不到「大客户」这个因素
_TOP5_RE = re.compile(r"前五名客户合计销售金额|前五名客户销售额")
_FX_RE = re.compile(r"^[(（]?[\d一二三四五六七八九十a-z]{0,2}[)）、.]?\s*[①②③④]?\s*(?:外汇|汇率)风险\s*$")
_RATE_RE = re.compile(r"^[(（]?[\d一二三四五六七八九十a-z]{0,2}[)）、.]?\s*[①②③④]?\s*利率风险\s*$")

# 每段到哪里为止：下一个同级或更高级的标题。风险章节里的条目是「1、」「（1）」，不会误停
_STOP = {
    "risk": re.compile(r"^(?:[(（][一二三四五六七八九十]{1,2}[)）]|[一二三四五六七八九十]{1,3}、|第[一二三四五六七八九十]+节)"),
    "mix": _COST_RE,
    "cost": re.compile(r"主要销售客户|主要供应商|前五名客户"),
    "fx": re.compile(r"利率风险|其他价格风险|流动性风险|信用风险|资本管理"),
    "rate": re.compile(r"外汇风险|汇率风险|其他价格风险|流动性风险|信用风险|资本管理"),
}
_STOP["risk_ref"] = _STOP["risk"]

# 每类段落最多带多少字给模型：够判断，又不至于一份年报喂进去几万字
BUDGET = {"risk": 6000, "risk_ref": 2500, "mix": 4500, "cost": 3500, "material": 1500,
          "top5": 600, "fx": 2000, "rate": 1500}
SECTION_NAMES = {"risk": "风险章节", "risk_ref": "重要风险提示", "mix": "主营业务构成",
                 "cost": "成本构成", "material": "主要原材料", "top5": "前五名客户与供应商",
                 "fx": "汇率风险（附注）", "rate": "利率风险（附注）"}


def _take(pages_lines: list[list[str]], page: int, line: int, budget: int,
          stop: re.Pattern | None = None) -> list[tuple[int, str]]:
    """从 (页, 行) 起往后取，最多 budget 字，遇到 stop（标题行本身除外）就停。
    返回 [(页码, 这一页取到的文字)]。"""
    out, left, first, stopped = [], budget, True, False
    for i in range(page, len(pages_lines)):
        kept = []
        for x in pages_lines[i][line if i == page else 0:]:
            s = x.strip()
            if not s:
                continue
            if stop and not first and stop.search(s):
                stopped = True
                break
            first = False
            kept.append(s)
        text = "\n".join(kept)[:left]
        if text:
            out.append((i + 1, text))
            left -= len(text)
        if stopped or left <= 0:
            break
    return out


def _first(pages_lines, pattern: re.Pattern, *, start: int = 0, line_match: bool = False):
    for i in range(start, len(pages_lines)):
        for j, ln in enumerate(pages_lines[i]):
            s = ln.strip()
            if (pattern.match(s) if line_match else pattern.search(s)):
                return i, j
    return None


def locate_sections(pages_lines: list[list[str]]) -> dict[str, list[tuple[int, str]]]:
    """{段落名: [(页码, 文字)]}。找不到的段落不出现在结果里。

    风险章节跳过前 5 页（第 2–3 页常有「详见……可能面对的风险」的引用句）；
    汇率 / 利率风险只在后半本找（前面管理层讨论里也会出现这两个词，但没有敏感性数字）。
    """
    found: dict[str, list[tuple[int, str]]] = {}
    n = len(pages_lines)
    hit = (_first(pages_lines, _RISK_RE, start=min(5, n), line_match=True)
           or _first(pages_lines, _RISK_FALLBACK_RE, start=min(5, n), line_match=True))
    if hit:
        found["risk"] = _take(pages_lines, *hit, BUDGET["risk"], _STOP["risk"])
        head = found["risk"][0][1][:300] if found["risk"] else ""
        if _RISK_REF_RE.search(head):            # 牧原：风险写在第一节的「重要风险提示」里
            ref = _first(pages_lines[:6], re.compile("重要风险提示"))
            if ref:
                found["risk_ref"] = _take(pages_lines, *ref, BUDGET["risk_ref"], _STOP["risk_ref"])
    for key, pattern in (("mix", _MIX_RE), ("cost", _COST_RE), ("material", _MATERIAL_RE),
                         ("top5", _TOP5_RE)):
        hit = _first(pages_lines, pattern, start=min(5, n))
        if hit:
            found[key] = _take(pages_lines, *hit, BUDGET[key], _STOP.get(key))
    third = n // 3
    for key, pattern in (("fx", _FX_RE), ("rate", _RATE_RE)):
        hit = _first(pages_lines, pattern, start=third, line_match=True)
        if hit:
            found[key] = _take(pages_lines, *hit, BUDGET[key], _STOP[key])
    return {k: v for k, v in found.items() if v}


# ---------------------------------------------------------------- 模型挑因素

PROMPT = """你是帮散户读年报的分析助手。下面是一份 A 股定期报告里的几段原文（每段标了页码）。
找出最能左右这家公司业绩的外部因素，最多 {max_n} 个，按对业绩影响从大到小排序。只输出 JSON。

什么算「因素」：公司外部的、会变化的、投资者能持续观察到的东西 ——
原材料或能源价格、产品售价、汇率、利率、下游需求、行业政策、大客户，或其他重大外部条件（来水、疫病等）。
公司内部管理类的风险（人才流失、内控、管理半径、安全生产）不算，除非原文明确说它对业绩影响重大。
影响明显很小的不要列（例如只占成本几个百分点的项目）；宁可少列，不要凑数。
前五名客户合计占比很高（超过一半）时，「大客户」本身就是一个因素。

每个因素：
- type：从 {types} 里选一个
- name：简短名称，例如「铜价」「生猪售价」「美元汇率」「风电装机需求」「药品集采」
- how：一句话说清它怎么影响这家公司（往哪个方向变对公司不利；原文说了能否转嫁、有没有套期保值就写上）。
  只写原文支持的内容，不要自己推测；句子里出现的数字必须是原文里原样出现的数字
- evidence：1–2 条原文引用，每条 {{"page": 页码, "quote": "逐字照抄原文，不超过 40 字"}}。必须一字不差地照抄
- exposure：原文里能说明这个因素影响有多大的数字，例如成本占比、收入占比、敏感性分析金额。
  写成 {{"page": 页码, "text": "一句话，例如：直接材料占风电业务成本的 70.53%", "value": "原文里的那个数字，逐字照抄"}}。
  原文没有这样的数字就填 null，绝对不要自己计算或估算
- market：如果这个因素对应下面某个可观察的行情，填它的代码；对应不上就填空字符串
  {markets}

输出格式：{{"drivers": [{{"type": "", "name": "", "how": "", "evidence": [], "exposure": null, "market": ""}}]}}"""


def _markets_line() -> str:
    return "；".join(f"{code}={name}" for code, (name, _) in MARKETS.items())


def sections_text(sections: dict[str, list[tuple[int, str]]]) -> str:
    """给模型看的原文：按段落分组，每页标出页码。同一页在多个段落里出现时只给一次。"""
    seen: set[tuple[int, str]] = set()
    parts = []
    for key in BUDGET:
        chunks = [(p, t) for p, t in sections.get(key, []) if (p, t[:80]) not in seen]
        if not chunks:
            continue
        seen.update((p, t[:80]) for p, t in chunks)
        parts.append(f"【{SECTION_NAMES[key]}】")
        parts.extend(f"〔第 {p} 页〕\n{t}" for p, t in chunks)
    return "\n\n".join(parts)


def ask_model(sections: dict, *, client, company: str = "", year: str = "",
              attempts: int = 2) -> list[dict] | None:
    """调模型，失败重试一次。仍失败（超时、推理吃光 token 导致正文为空、不是 JSON）返回 None。
    评测里见过偶发失败：同一份长江电力，一次 44 秒后失败，紧接着单独重跑两次都成功。"""
    who = f"{company} {year} 年报".strip()
    messages = [{"role": "system", "content": PROMPT.format(
                    max_n=MAX_DRIVERS, types="、".join(TYPES), markets=_markets_line())},
                {"role": "user", "content": (f"【公司】{who}\n\n" if who else "") + sections_text(sections)}]
    for _ in range(attempts):
        try:
            resp = client.chat.completions.create(
                model=config.LLM_MODEL, messages=messages, temperature=0,
                max_tokens=config.DRIVERS_MAX_TOKENS, timeout=config.DRIVERS_TIMEOUT,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "")
        except Exception:
            continue
        drivers = data.get("drivers") if isinstance(data, dict) else None
        if isinstance(drivers, list) and drivers:
            return drivers
    return None


# ---------------------------------------------------------------- 程序核对

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _squash(text: str) -> str:
    """比对用：去掉所有空白和常见的全半角差异。PDF 拍平后原文里随处断行、夹空格。"""
    t = re.sub(r"\s+", "", text or "")
    return t.translate(str.maketrans("（）：；，", "():;,"))


def _page_has(flat: list[str], page: int, needle: str, spread: int = 1) -> int | None:
    """needle 在第 page 页（允许前后 spread 页，引用可能跨页）里出现，返回实际所在页码。"""
    n = _squash(needle)
    if not n:
        return None
    for p in [page] + [page + d for k in range(1, spread + 1) for d in (-k, k)]:
        if 1 <= p <= len(flat) and n in flat[p - 1]:
            return p
    return None


_CLAUSE_RE = re.compile(r"(?<=[，；。,;])")


def _numbers_ok(text: str, pool: str) -> bool:
    return all(_squash(x) in pool for x in _NUM_RE.findall(text))


def verified_how(how: str, pool: str) -> str:
    """说明里的数字必须在给模型看的原文里出现过。按分句检查，带着找不到的数字的分句删掉。
    实测：长江电力那条写了「影响净利润约 6,780.03 万元」，而暴露度一栏是空的 —— 这种数不能放过。"""
    kept = [c for c in _CLAUSE_RE.split(how) if c and _numbers_ok(c, pool)]
    return "".join(kept).strip("，；,; ")


def verify(raw: list, pages_lines: list[list[str]], sections: dict | None = None) -> list[dict]:
    """模型的输出 → 核对过的因素。引用找不到的删掉；一条引用都不剩的因素不要；
    暴露度的数字必须在那一页原文里出现，否则只删暴露度；说明里的数字要在给模型的原文里出现；
    行情代码必须在白名单里。"""
    flat = [_squash("".join(lines)) for lines in pages_lines]
    pages = sorted({p for v in (sections or {}).values() for p, _ in v})
    pool = "".join(flat[p - 1] for p in pages if 1 <= p <= len(flat)) if pages else "".join(flat)
    out = []
    for d in raw or []:
        if not isinstance(d, dict) or not str(d.get("name") or "").strip():
            continue
        evidence = []
        for ev in d.get("evidence") or []:
            try:
                page, quote = int(ev.get("page")), str(ev.get("quote") or "").strip()
            except (AttributeError, TypeError, ValueError):
                continue
            real = _page_has(flat, page, quote) if 4 <= len(_squash(quote)) <= 80 else None
            if real:
                evidence.append({"page": real, "quote": quote})
        if not evidence:
            continue
        exposure = None
        ex = d.get("exposure")
        if isinstance(ex, dict):
            try:
                page = int(ex.get("page"))
            except (TypeError, ValueError):
                page = 0
            value, text = str(ex.get("value") or "").strip(), str(ex.get("text") or "").strip()
            nums = _NUM_RE.findall(value)
            # 暴露度的每个数字都要在原文那一页找到；说明文字里出现的数字也必须是这些数
            if nums and text and all(_page_has(flat, page, x, spread=0) for x in nums) \
                    and set(_NUM_RE.findall(text)) <= set(nums):
                exposure = {"page": page, "text": text[:80], "value": value[:30]}
        market = str(d.get("market") or "").strip()
        out.append({
            "type": d.get("type") if d.get("type") in TYPES else "其他",
            "name": str(d["name"]).strip()[:20],
            "how": verified_how(str(d.get("how") or "").strip(), pool)[:160],
            "evidence": evidence[:2],
            "exposure": exposure,
            "market": market if market in MARKETS else "",
        })
        if len(out) >= MAX_DRIVERS:
            break
    return out


# ---------------------------------------------------------------- 汇总与缓存

def build_from_lines(pages_lines: list[list[str]], *, client, company: str = "",
                     year: str = "", period: str = FY) -> dict | None:
    sections = locate_sections(pages_lines)
    if not sections:
        return None
    raw = ask_model(sections, client=client, company=company, year=year)
    if raw is None:
        return None
    return {"version": DRIVERS_VERSION, "period": period,
            "sections": {k: [p for p, _ in v] for k, v in sections.items()},
            "drivers": verify(raw, pages_lines, sections), "proposed": len(raw)}


def load_or_build(doc_id: str, *, client) -> dict | None:
    """读 data/<doc_id>/drivers.json；没有或版本不对就现做（一次模型调用）并写回。
    模型调用失败不写缓存，下次再试。"""
    folder = config.DATA_DIR / doc_id
    path = folder / DRIVERS_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("version") == DRIVERS_VERSION:
            return cached
    except (OSError, ValueError):
        pass
    pdf = folder / "source.pdf"
    if not pdf.exists():
        return None
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    from .pdf_parser import raw_page_lines

    out = build_from_lines(raw_page_lines(pdf.read_bytes(), max_pages=10 ** 4), client=client,
                           company=meta.get("company", ""), year=str(meta.get("year", "")),
                           period=meta.get("period") or FY)
    if out:
        try:
            path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return out
