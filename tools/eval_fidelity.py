"""数字可溯源性检查：回答里的每个数字，能不能在检索到的片段里找到出处。

这是整套测量里唯一**不需要人工判分、也不依赖文本套路**的指标，对财报工具也最重要 ——
一个编造数字的"深度"回答，比一个简短但正确的回答危险得多。

刻意不做的事：不判断"深度"、不判断"答得好不好"。那些没有标准答案，靠正则去猜
只会自欺（这个项目上已经栽过一次：用 `未找到|未披露` 匹配弃答，把
「未对…作专门说明，**但从片段可以看出**…」这种有数据的回答误判成了弃答）。
这里只回答一个能给出确定答案的问题：**这个数字有没有出处。**
"""
from __future__ import annotations

import re

# 元 -> 显示单位的换算。年报里金额常写成「246.74 亿元」或「24,673,670,269.16 元」，
# 两者指同一个数，所以核对时必须换算到同一量纲再比。
UNIT_SCALE = {"亿元": 1e8, "万元": 1e4, "千元": 1e3, "元": 1.0}
_UNIT_RE = "|".join(sorted(UNIT_SCALE, key=len, reverse=True))

# 数字，可选后面跟一个单位。百分号单独处理。
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(" + _UNIT_RE + r"|%|％)?")
_PLAIN_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _decimals(raw: str) -> int:
    return len(raw.split(".")[1]) if "." in raw else 0


def extract_numbers(text: str) -> list[dict]:
    """抽出文本里的数字，带上单位和显示精度。

    精度是必须的：回答写「246.74 亿元」，片段里是 24,673,670,269.16 元 ——
    两个数并不相等，只是四舍五入到显示的两位。所以比对时容差要按显示精度给。
    """
    out = []
    for m in _NUM_RE.finditer(text):
        raw, unit = m.group(1), m.group(2)
        # 「2025年」这类年份不是财务数字，单独标出来避免误判
        if unit is None and _decimals(raw) == 0 and 1900 <= _to_float(raw) <= 2100:
            out.append({"raw": raw, "unit": unit, "value": _to_float(raw), "dec": 0,
                        "kind": "year"})
            continue
        if unit in ("%", "％"):
            out.append({"raw": raw, "unit": "%", "value": _to_float(raw), "dec": _decimals(raw),
                        "kind": "percent"})
            continue
        scale = UNIT_SCALE.get(unit, 1.0) if unit else 1.0
        out.append({"raw": raw, "unit": unit, "value": _to_float(raw) * scale,
                    "dec": _decimals(raw), "scale": scale, "kind": "amount"})
    return out


# 表格自己声明量纲的写法：「单位：万元 币种：人民币」。
# 不认这个的话，模型把 `2,147,054.45` 万元写成「214.71 亿元」会被判成编造 ——
# 而它是对的。**这个 bug 只有在换一份年报时才暴露**：内置示例年报的报表以元为单位，
# 裸数字正好等于答案里的原值；换成以万元为单位的年报，整张表都被误判。
_UNIT_DECL = re.compile(r"单位\s*[：:]\s*(" + _UNIT_RE + r")")


def _chunk_index(texts: list[str]) -> dict:
    """把片段里的数字建成索引。

    带单位的数字同时存原值和换算到元的值：片段写「24,673,670,269.16」而回答写
    「246.74 亿元」，换算后两者才能对上。

    **裸数字要按所在片段声明的量纲换算。** 报表常常在表头写「单位：万元」，
    行里就只有 `2,147,054.45`。不换算的话，答案里正确的「214.71 亿元 ／ 2,147,054.45 万元」
    一条都对不上，会被整片判成编造。

    amounts 排好序，是为了用二分做容差查找 —— 朴素两两比较是 O(n²)，
    而这里每验一个数字都要跑一遍。
    """
    amounts: list[float] = []
    percents: set[str] = set()
    for text in texts or []:
        text = text or ""
        decl = _UNIT_DECL.search(text)
        scale = UNIT_SCALE[decl.group(1)] if decl else 1.0
        for m in _NUM_RE.finditer(text):
            raw, unit = m.group(1), m.group(2)
            if unit in ("%", "％"):
                percents.add(raw.replace(",", ""))
                continue
            value = _to_float(raw)
            amounts.append(value)
            if unit:
                amounts.append(value * UNIT_SCALE[unit])
            elif scale != 1.0:
                amounts.append(value * scale)
    amounts.sort()
    return {"amounts": amounts, "percents": percents}


def _near(values: list[float], target: float, tol: float) -> bool:
    """排序数组里有没有落在 target ± tol 内的值。"""
    import bisect
    i = bisect.bisect_left(values, target - tol)
    return i < len(values) and values[i] <= target + tol


def _tolerance(value: float, dec: int, scale: float) -> float:
    """容差 = 显示精度的一半（246.74 亿 -> ±0.005 亿）。

    **不要在这里加相对余量。** 曾经写成 max(half_ulp, |value|*1e-6)，对 88.9 亿
    这种大数会给出 ±8889 的容差；推导时要在片段里两千个数字中找匹配，
    这么松几乎必然撞上某个值 —— 实测一个凭空编造的 8,888,888,888.88 就这么混了过去。
    直接匹配时两边本来就该相等，差异只来自四舍五入，半格精度正好。
    """
    half_ulp = 0.5 * (10 ** -dec) * scale
    return max(half_ulp, 1e-9)


def _found(value: float, dec: int, scale: float, index: dict) -> bool:
    return _near(index["amounts"], value, _tolerance(value, dec, scale))


def _derivable(target: float, dec: int, scale: float, pool: list[float],
               rates: list[float]) -> bool:
    """这个数能不能用**片段里**的数字算出来。

    推导池是片段里的数字，不是回答里"有出处"的那些。实测踩过：
    「营业收入增加 2,351,090,338.18 元」这句话里只有结果、没有两个操作数，
    拿回答自身做池子的话池子是空的，合法的推算值会被误报成编造。

    只支持语义明确的运算 —— 每多一条规则都会削弱「抓编造」的能力：
      · 差 / 和 / 比值 / 占比        单步四则
      · 用有出处的增长率反推上期值    回答常写「上期约 202.98 亿」，只有这种算法对得上
      · 二阶：把推出来的上期值再拿去算差（「增加约 21.92 亿」= 本期 − 上期）

    容差按**误差传播**给，不能共用目标的容差：解 b = a/target 时，target 的舍入
    误差传到 b 上会被放大 a/target² 倍。典型例子是「约为净利润的 3.98 倍」——
    真值 3.9759，目标容差只有 5e-5，但反解出来的除数容差有几千，共用一个容差必然误判。

    已知局限：多项相加的合计验不出来（「上述项目合计约 2.40 亿元」是 7 项之和）。
    这是有意的取舍 —— 枚举子集代价太高，报出来让人看一眼比漏报安全。
    实现上走「解方程 + 二分查找」，把朴素两两比较从 O(n²) 降到 O(n log n)。
    """
    if not pool:
        return False
    tol = _tolerance(target, dec, scale)

    def near(v: float, t: float) -> bool:
        return _near(pool, v, max(t, 1e-9))

    for a in pool:
        # a - b = target  ->  b = a - target
        if near(a - target, tol):
            return True
        # a + b = target  ->  b = target - a
        if near(target - a, tol):
            return True
        # 比值与占比必须排掉 0：片段里大量存在「0.00」，于是 0/target = 0 会和池子里的 0
        # 匹配上，容差还跟着塌成 0 —— 实测任何编造的数字都能靠这一条蒙混过关。
        #
        # 比值还按数量级门控：a/b 的结果是无量纲的小数，只有「倍数/占比」这类目标才可能
        # 由它推出来。不设这道门，「999.99 亿元」这种大额会靠 246.7亿 / 0.2467 蒙混过去。
        if a and target:
            if abs(target) < 1e4:
                # a / b = target  ->  b = a / target，误差放大 |b/target| 倍
                b = a / target
                if abs(b) > 1e-9 and near(b, abs(b) * tol / abs(target)):
                    return True
            # a * b / 100 = target  ->  b = 100 * target / a，误差放大 100/|a| 倍
            # b 是百分比，只能在 0-100 之间 —— 不加这道门会算出 416% 这种不可能的值，
            # 算术上却成立，于是「999.99 亿元」也能蒙混过去。
            b2 = 100.0 * target / a
            if 0 < b2 <= 100 and near(b2, 100.0 * tol / abs(a)):
                return True
        for p in rates:
            if p <= -100:
                continue
            factor = 1 + p / 100.0
            # a / (1 + p/100) = target  ->  a = target * (1 + p/100)
            if near(target * factor, abs(factor) * tol):
                return True
            # 二阶：(a - d) = target，其中 d = a'/(1 + p/100)
            #   消去 d 得 a' = (a - target)(1 + p/100)
            if near((a - target) * factor, abs(factor) * tol):
                return True
    return False


def check(answer: str, hits: list[dict]) -> dict:
    """回答里的数字有多少能在命中片段里找到出处，或者能用有出处的数字算出来。

    只看两件事：**有没有出处**、**能不能推导**。都是能给出确定答案的问题。
    不判断"深度"、不判断"答得好不好"—— 那些没有标准答案，靠正则猜只会自欺。
    """
    # context_header 里常常就写着「单位：元」—— 它是检索层为缺表头的报表片段补上的，
    # 拼回去才能判断这一片标的量纲。
    index = _chunk_index([(h.get("context_header") or "") + "\n" + (h.get("text") or "")
                          for h in hits])
    nums = extract_numbers(answer)
    amounts = [n for n in nums if n["kind"] == "amount"]
    percents = [n for n in nums if n["kind"] == "percent"]

    sourced, unsourced = [], []
    for n in amounts:
        (sourced if _found(n["value"], n["dec"], n.get("scale", 1.0), index) else unsourced).append(n)

    # 只有**有出处**的百分比才能当增长率用 —— 拿一个编造的百分比去反推，
    # 推出来的数自然也对不上，不影响判断
    rates = [n["value"] for n in percents if n["raw"].replace(",", "") in index["percents"]]

    derived, orphan = [], []
    for n in unsourced:
        # 推导池是**片段里的**数字，不是回答里有出处的那些
        if _derivable(n["value"], n["dec"], n.get("scale", 1.0), index["amounts"], rates):
            derived.append(n)
        else:
            orphan.append(n)

    pct_orphan = [n for n in percents if n["raw"].replace(",", "") not in index["percents"]]

    return {
        "金额个数": len(amounts),
        "有出处": len(sourced),
        "可推导": len(derived),
        "无出处": len(orphan),
        "无出处样例": [n["raw"] + (n["unit"] or "") for n in orphan][:6],
        "百分比个数": len(percents),
        "无出处百分比": len(pct_orphan),
        "有据可依率": round((len(sourced) + len(derived)) / max(1, len(amounts)), 3),
    }
