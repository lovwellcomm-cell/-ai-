"""深度度量：把「有深度的回答」那份细则翻译成能跑的检查。

这个模块只做一件事 —— **逐条回答：那份细则里的每一条，在这个系统里到底能不能被机器判定。**
读它的人要能一眼看出哪些结论可信、哪些只是代理信号、哪些根本没测。

## 拿实测数据换来的取舍

设计每一条检查时，都先拿 30 份真实回答（5 组配置 × 6 道题，401 句 / 1099 个金额 / 215 个条目）
跑过一遍，**凡是误报压不下去的就砍掉**。被砍掉的比留下的多，这是有意的：

| 曾想做的检查 | 实测结果 | 结论 |
|---|---|---|
| 越界引用 | 30/30 回答里 0 个越界标记 → 零误报 | 留 |
| 引文核对（全部引号） | 58 条引文里 18 条对不上，**全是合法的**（引自己的结论、引用用户原话） | 收窄到转述式引文 |
| 换算一致率（要求每个亿元都带原值） | 280 处单位里 42 处（15%）合法地不带 | 砍掉这一半 |
| 算式可复现率 | 比 `eval_fidelity._derivable` 严格更弱：句子级误报、条目级漏报、回答级恒真 | 砍 |
| 断言率 | 215 个条目里被标出的**全部合法**（结论行、结构标签、口径说明） | 降级为清单 |
| 归因有据率 | 17 个被标出的因果句，**16 个是模型在正确地说"年报未披露原因"** | 降级为清单 |
| 空洞句黑名单 | 30 份回答里 0 命中 —— 纯空转 | 砍 |
| 空洞句冗余检测 | 去掉数字后有 49/401 被标出，**全是承载最新数字的表格行**，与信息量反相关 | 砍 |

## 输出里几乎没有"率"

所有信号都出 **计数 + 清单**，不出百分比 —— 唯一的例外是 `有据可依率`，
而它总是和分母 `金额个数` 印在同一行。理由是实测出来的：这个语料里
`引用命中率` 对「覆盖」那道题恒为 0.29，而那个 0.29 只是个排版产物
（`分点依据：` 这行被当成了一个没有引用的句子）。一个含分母的漂亮数字，
会在没人注意分母的时候变成结论。清单不会。

## 每条结论都要说清它测不到什么

- 准确性：测的是「**没超出检索到的片段**」，不是「事实正确」。年报本身有错、
  或模型把年报读反了，这里一条都抓不到。
- 完整性：只是**锚点下限**，实测横跨 5 组配置恒为全中，没有分辨力 —— 它永远不会骗人，
  但也永远区分不出好坏。
- 解释层次：**没有确定性实现**。而且方向是反的 —— 最好的「覆盖」回答恰恰是
  说「未披露」说得最多的那些。这一条只能人读。
"""
from __future__ import annotations

import re

# 容差只从 eval_fidelity 拿，不在这里另写一份 —— 两处各写一份容差，
# 正是这个项目上次栽跟头的方式（相对余量 1e-6 让 88.9 亿有了 ±8889 的容差）。
from tools.eval_fidelity import _tolerance, check as fidelity_check

# ---------------------------------------------------------------- 归一化

def _norm(s: str) -> str:
    """去掉空白、全角空格和 markdown 粗体星号。

    实测三种都必须处理：`**246.74亿元**（24,673,670,269.16元）` 的星号夹在值和括号之间；
    `“减少 0.13 个百分点”`（片段里带 ASCII 空格）不归一化就会被判成非原文；
    表格行里出现 U+3000。
    """
    return re.sub(r"[\s　*]+", "", s or "")


# ---------------------------------------------------------------- 1 准确性

# 引用标记。和 rag/generator.py:cited_numbers 用的是同一个形状，但那边**静默丢弃**越界编号，
# 于是"模型引用了不存在的来源"这件事在整条链路里不留痕迹。这里把它捞出来。
_BRACKET_RE = re.compile(r"\[([\d,，、\s]+)\]")


def citations(answer: str, n_chunks: int) -> dict:
    """越界引用 = 虚构来源。

    这是整套检查里唯一能抓「编造来源」（而不是编造数字）的一条，30 份真实回答上零误报。
    三道防误报的闸门都是真实存在的风险，只是眼下还没触发：
      · `[2025]` 会匹配上正则，而 2025 > 8 —— 别的提示词里模型就会给年份加方括号
      · `[1](https://…)` 是 markdown 链接，不是引用
      · 行首的 `[1] ` 是列表标记，不是引用
    """
    text = answer or ""
    in_range: set[int] = set()
    out_of_range: list[str] = []
    for m in _BRACKET_RE.finditer(text):
        if text[m.end():m.end() + 1] == "(":
            continue                                   # markdown 链接
        line_start = text.rfind("\n", 0, m.start()) + 1
        if not text[line_start:m.start()].strip() and text[m.end():m.end() + 1] in (" ", "　"):
            continue                                   # 行首列表标记
        for x in re.split(r"[,，、\s]+", m.group(1)):
            if not x.isdigit():
                continue
            n = int(x)
            if len(x) == 4 and 1900 <= n <= 2100:
                continue                               # 年份，不是引用编号
            (in_range.add(n) if 1 <= n <= n_chunks else out_of_range.append(m.group(0)))
    return {"引用编号": sorted(in_range), "越界标记": out_of_range, "越界数": len(out_of_range)}


# 转述式引文的框架词。只有紧跟在这些词后面、或后面紧跟 [n] 的引号，才算"声称是原文"。
_ATTR_FRAME = re.compile(r"(说明为|说明是|列为|列示|披露为|表述为|称|写明|记载为)$")
_QUOTE_RE = re.compile(r"“([^”]{1,120})”")


def quote_check(answer: str, hits: list[dict]) -> dict:
    """转述式引文是否逐字出现在片段里。

    **只查转述式引文。** 全量查引号实测 58 条里 18 条对不上（31%），而它们全都合法：
    引自己的结论（`“持续增长、增速放缓”`）、引用用户的问话（`“那去年呢”`）、
    引用提示词自己的词（`“本期值、上期值、变化额、变化率”`）。
    收窄到转述式之后是 15 条、1 条对不上，而那 1 条也靠下面的斜杠切分修好了。
    """
    blob = _norm("\n".join((h.get("text") or "") for h in (hits or [])))
    text = answer or ""
    checked, misses = 0, []
    for m in _QUOTE_RE.finditer(text):
        before = text[:m.start()].rstrip()
        after = text[m.end():m.end() + 1]
        if not (_ATTR_FRAME.search(before) or after == "["):
            continue
        checked += 1
        content = m.group(1)
        # `“营业总收入/营业收入”` 是拿两个行标签拼出来的，不是原文 —— 拆开逐段核对
        parts = [p for p in re.split(r"[/｜、]", content) if _norm(p)]
        if not parts or not all(_norm(p) in blob for p in parts):
            misses.append(content)
    return {"转述引文数": checked, "非原文": misses, "非原文数": len(misses)}


# ---------------------------------------------------------------- 3 逻辑链

# 数值 + 单位。前面的负号必须一起捕获 —— 见 unit_conversions 的说明。
_UNIT_OCC_RE = re.compile(r"(-?)(\d[\d,]*(?:\.\d+)?)(亿元|万元|千元|元)")
_UNIT_SCALE = {"亿元": 1e8, "万元": 1e4, "千元": 1e3, "元": 1.0}
_INNER_RE = re.compile(r"^(-?)(\d[\d,]*(?:\.\d+)?)(亿元|万元|千元|元)?")
# 「少 / 低 / 净流出」这类方向词说明外侧写的是差额的大小而非带符号的值。
# `高`/`低` 单字也要：实测「2024年则**低**11.66亿元（-1,166,119,050.51元）」
# 只写「低于」覆盖不到，于是又把正确的带符号原值判成不一致。
_DIRECTION = re.compile(r"(少|低于|高|低|下降|减少|负|缺口|差额|净流出|净增加)$")


def unit_conversions(answer: str) -> dict:
    """`246.74亿元（24,673,670,269.16元）` 这种「四舍五入值 + 括号原值」对不对得上。

    这是细则第 3 条「不得自相矛盾」目前唯一完全可判定的形式。提示词第 6 条强制模型
    写成这个格式，所以每一处金额上这对数据都必然存在。实测 233 对，233 对全对：
    **这个房子的算术是扎实的**，配错年份、抄错一位、把上期当本期，全都会在这里现形。

    只做一致性这一半。**不做**「每个亿元都必须带括号原值」—— 实测 280 处单位里
    有 42 处（15%）合法地不带：结论行、推导值（`较2024年增加23.51亿元`）、
    列举（`分别为16.04亿元、3.60亿元`），硬要求会大面积误报。

    **负号必须传播。** 不传播的话 207 对里误报 19 对，全部是负数，而
    `-5.89亿元（-589,153,155.66元）` 正是「跨表」那道题最核心的数字 ——
    一个把最关键的负号判成错的检查，比没有检查更糟。
    """
    text = _norm(answer)
    paired = consistent = 0
    bad: list[str] = []
    note_only: list[str] = []
    no_paren: list[str] = []

    for m in _UNIT_OCC_RE.finditer(text):
        shown = m.group(0)
        rest = text[m.end():]
        # 只为「换算后的单位」统计这两个桶。单看「元」的话，回答里每一个原始数值
        # （`24,673,670,269.16元`）都会落进「无括号」，197 条噪声把真正要看的那几条淹掉。
        converted = m.group(3) != "元"
        if not rest.startswith("（"):
            if converted:
                no_paren.append(shown)
            continue
        close = rest.find("）")
        inner = rest[1:close] if close > 0 else rest[1:]
        im = _INNER_RE.match(inner)
        if not im:
            if converted:
                note_only.append(shown)   # 括号里是说明文字（`（据片段数字计算）`），不是原值
            continue
        paired += 1
        # 千分位逗号必须去掉：`17,905,865,235.94` 直接 float() 会 ValueError
        raw = m.group(2).replace(",", "")
        outer = float(m.group(1) + raw) * _UNIT_SCALE[m.group(3)]
        inner_raw = im.group(2).replace(",", "")
        iv = float(im.group(1) + inner_raw) * _UNIT_SCALE.get(im.group(3) or "元", 1.0)

        # 容差取两侧里**较松**的那个。实测house style 两种顺序都会写：
        #   `238.83亿元（23,882,535,292.17元）`  —— 四舍五入在外侧
        #   `23,882,535,292.17元（238.83亿元）`  —— 四舍五入在括号里
        # 只按外侧算的话，第二种的容差是 ±0.005 **元**，等于要求分毫不差，
        # 于是「238.83亿元 ≈ 23,882,535,292.17元」这个正确答案被判成不一致。
        def _tol(val_raw: str, scale: float) -> float:
            d = len(val_raw.split(".")[1]) if "." in val_raw else 0
            return _tolerance(float(val_raw), d, scale)

        tol = max(_tol(raw, _UNIT_SCALE[m.group(3)]),
                  _tol(inner_raw, _UNIT_SCALE.get(im.group(3) or "元", 1.0)))

        # 符号问题的另一面：实测 `比净利润少11.66亿元（-1,166,119,050.51元）` ——
        # 外侧写的是差额的**大小**，括号里给的是**带符号**的值，两者符号相反但都对。
        # 所以只有在正前方出现方向词时，才允许按绝对值比对；否则必须同号，
        # 不然 `-5.89亿元（589,153,155.66元）` 这种真的抄错符号会被放过。
        before = text[max(0, m.start() - 12):m.start()]
        if abs(outer - iv) <= tol + 1e-9 or (
            _DIRECTION.search(before) and abs(abs(outer) - abs(iv)) <= tol + 1e-9
        ):
            consistent += 1
        else:
            bad.append(f"{shown}（{inner[:32]}）")

    return {
        "配对数": paired,
        "换算一致": consistent,
        "换算不一致": bad,
        "仅说明括号": note_only[:8],       # 供人看，不是失败
        "无括号": no_paren[:8],            # 供人看，不是失败
    }


# 「据片段数字计算」的各种真实写法，实测至少有六种表面形式：
#   （据片段数字计算） ｜ 据片段数字计算增长约24.67% ｜ （据[5]数字计算）
#   （据[1][2][7]数字计算） ｜ 据片段[1][7]数字计算 ｜ （据“减少 0.13 个百分点”计算）
_MARK_CORE = r"据[^）)。，；\n]{0,20}(?:计算|给出|推断)"
# 必须**紧邻**才算这个比率的标记。早先用 26 字的宽松窗口，结果
# `…增加30.64亿元（3,064,235,972.99元，据[1][2][7]数字计算），变动比例为520.11%[7]`
# 里属于"30.64亿元"的那个标记，被算到了照抄来的"520.11%"头上，凭空造出 3 个误报。
_MARK_BEFORE = re.compile(_MARK_CORE + r"[）)]?[，,]?(?:为|约|达|是)?$")
_MARK_AFTER = re.compile(r"^[（(]?" + _MARK_CORE + r"[）)]?")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|％)")
# 水平值不是增长率：毛利率 5.83% 说的是"是多少"，不是"涨了多少"
_LEVEL_RATIO = re.compile(r"(毛利率|占比|比例|资产负债率|费率)$")


def _reproducible(rate: float, pool: list[float]) -> bool:
    """回答自己引用的数字里，有没有哪一对能算出这个比率。"""
    r = abs(rate)
    for a in pool:
        for b in pool:
            if b == 0:
                continue
            for v in ((a - b) / b * 100.0, (a - b) / abs(b) * 100.0):
                if abs(abs(v) - r) <= 0.005:      # 0.005 个百分点
                    return True
    return False


# 带**前导负号**的金额。
_OPERAND_RE = re.compile(r"(-?)(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|千元|元|%|％)?")


def _operands(text: str) -> list[float]:
    """回答里的金额，**带符号**。

    不能直接用 eval_fidelity.extract_numbers —— 它的正则不含前导负号，
    `-589,153,155.66` 会被抽成 +589,153,155.66。后果实测过：
    `2024年比2023年减少10.28亿元，下降234.21%` 是对的
    （−589,153,155.66 与 438,962,688.40 算出 −234.214%），
    但池子里两个数都成了正数，反解出 +34.21%，于是被判成算错。
    同一个负号问题在换算检查里也出现过一次，这里是第二次。
    """
    out = []
    for m in _OPERAND_RE.finditer(text or ""):
        unit = m.group(3)
        if unit in ("%", "％"):
            continue
        raw = m.group(2).replace(",", "")
        if unit is None and "." not in raw and len(raw) == 4 and 1900 <= float(raw) <= 2100:
            continue                                  # 年份不是金额
        v = float(m.group(1) + raw) * _UNIT_SCALE.get(unit or "元", 1.0)
        if v:
            out.append(v)
    return sorted({round(v, 4) for v in out})[:150]


def computed_rates(answer: str) -> dict:
    """模型**自己算**的比率，能不能用回答自己引用的数字复现出来。

    实测抓到过一个真的算错：followup/跨表 写「增幅约 15.56%」，
    而同一份回答引用的 3,536,050,047.18 / 22,739,727,998.27 = **15.5501%**，
    baseline 对同一个量算的就是 15.55%。差 0.01 个百分点，是真错。

    只查紧邻「据…计算」标记的比率 —— 没标记的是照抄片段，归片段管。
    排除两类：
      · **百分点**：`5.96%（据"减少 0.13 个百分点"计算）` 是由百分点变化推水平值，
        不是比率运算，本检查不建模这种算法，硬套会误报
      · **毛利率/占比/比例**：那是水平值，不是增长率

    **它抓不全，这一点必须说清：** 回答里数字多的时候，某一对无关的数字有可能
    偶然凑出这个比率，于是真错误被漏掉。这个方向是安全的（不会误告），
    但「自算不一致数 = 0」不等于"没有算错"。
    """
    text = answer or ""
    pool = _operands(text)
    checked, bad = 0, []
    for m in _PCT_RE.finditer(text):
        before = text[max(0, m.start() - 30):m.start()]
        after = text[m.end():m.end() + 30]
        mk_b, mk_a = _MARK_BEFORE.search(before), _MARK_AFTER.search(after)
        if not (mk_b or mk_a):
            continue
        marker = (mk_b.group(0) if mk_b else "") + (mk_a.group(0) if mk_a else "")
        if "个百分点" in marker:
            continue                       # 由百分点推出来的水平值，不是比率
        if after.startswith("个百分点"):
            continue
        if _LEVEL_RATIO.search(text[max(0, m.start() - 8):m.start()]):
            continue
        checked += 1
        if not _reproducible(float(m.group(1)), pool):
            bad.append({"率": m.group(1) + "%", "上下文": (before + m.group(0) + after)[-46:]})
    return {"自算率检查数": checked, "自算不一致": bad, "自算不一致数": len(bad)}


# ---------------------------------------------------------------- 4 证据（只出清单）

# 这些开头的条目本来就**不该**带引用：结论行、结构标签、口径说明、反问用户的话。
# 不排掉的话，实测 215 个条目里被标出的全部合法 —— 一个全部误报的指标。
_NOT_A_CLAIM = re.compile(
    r"^((一句话)?结论[：:]|分点依据|依据(如下)?[：:]?|分项(如下)?[：:]?|以上(均为|都是|为)|口径[：:]|"
    r"补充[：:]|说明[：:]|注[：:]|总结[：:]|综合(来看|看)[：:]|因此|所以|"
    r"请(明确|补充|说明|指出)|若|如果|即|其中)"
)
_ITEM_RE = re.compile(r"^\s*(?:[-*•]\s*|\d+[.、]\s*|\*\*[^*]+\*\*\s*$)")


def unsourced_items(answer: str) -> dict:
    """既没有引用、也没有可溯源数字的条目。

    **只出清单，不出比例。** 细则第 4 条说「断言不得多于有证据的观点」，听起来像个比率，
    但实测两种口径都错：按"任何数字都算证据"，`结论：营业收入三年上升，2025年增速低于2024年。`
    会因为句里有年份而通过 —— 而它恰恰是没有引用的断言；按"必须有可溯源数字"，
    所有结论行、结构标签、口径说明全被误报。所以这里退一步：
    只把"看着像观点、却一点依据都不带"的条目标出来给人看。
    """
    items = [ln.strip() for ln in (answer or "").splitlines() if ln.strip()]
    flagged = []
    for it in items:
        body = _ITEM_RE.sub("", it).strip()
        if not body or _NOT_A_CLAIM.match(_norm(body)):
            continue
        if _BRACKET_RE.search(it):        # 有引用
            continue
        if re.search(r"\d[\d,]*\.?\d*", it):   # 有数字（含年份 —— 刻意放宽，宁漏勿误）
            continue
        flagged.append(body)
    return {"无引用条目": flagged, "无引用条目数": len(flagged), "条目总数": len(items)}


# ---------------------------------------------------------------- 5 解释层次（只出清单）

_CAUSAL_RE = re.compile(r"(因为|由于|主要系|主要是|原因|导致|使得|驱动|所致|受[^。；\n]{0,10}影响)")
# 说"没有原因"的句子不是归因。实测 17 个被标出的因果句里，16 个是模型在正确地
# 报告"年报未披露原因" —— 不过滤的话，这个检查专门惩罚诚实。
_NEGATION_RE = re.compile(r"(未|没有|无法|不能|不足以|无[^。；\n]{0,4}(?:说明|披露|解释))")


def causal_sentences(answer: str) -> dict:
    """没有出处的归因句（清单）。

    **这一条不是细则第 5 条的度量。** 第 5 条「有没有解释为什么」在这里没有诚实的确定性实现，
    而且方向是反的：最好的「覆盖」类回答恰恰是说「年报未披露原因」说得最多的那些，
    任何"看它有没有讲因果"的启发式都会把它们判成最浅。
    这里保留的只是一根**绊线**：当模型确实给了一个归因、却没给任何依据时，把它挑出来。
    金融工具最怕的就是这种句子（"毛利率下降主要因为铜价上涨"却不给依据）。
    """
    out = []
    for s in re.split(r"[。；\n]", answer or ""):
        s = s.strip()
        if not s or not _CAUSAL_RE.search(s):
            continue
        if _NEGATION_RE.search(s):
            continue                       # 在说"没有原因"，不是归因
        if _BRACKET_RE.search(s) or re.search(r"\d[\d,]*\.?\d*", s):
            continue
        out.append(s)
    return {"无据归因": out, "无据归因数": len(out)}


# ---------------------------------------------------------------- 2 完整性（条件触发）

_CALIBER_NOTE = re.compile(r"(母公司|个别报表|非合并)(口径|报表)")

# 母公司口径，但"归属于母公司股东的净利润"里也有"母公司"四个字 —— 后者是固定科目名，
# 每篇回答都有，不排除掉的话这条检查会被刷成"永远通过"。
# 实测 30 份回答里 `母公司口径` 只出现 1 次，而含"母公司"的行随处可见。


def caliber(answer: str, hits: list[dict]) -> dict:
    """只找到母公司口径时，回答有没有注明。

    提示词第 3 条的触发条件其实是「**只找到**母公司口径时」才要求注明，
    所以不能写成"回答里有没有'母公司'"。条件触发：
    **引用了母公司片段、且没引用任何合并片段**，才要求口径说明。

    `hit_subsection` 实测取值含 `母公司利润表` / `母公司现金流量表` / `母公司所有者权益变动表`，
    触发条件是拿得到的。
    """
    hits = hits or []
    if not hits:
        return {"口径触发": False, "口径缺注": None}
    cited = citations(answer, len(hits))["引用编号"]
    subs = [(hits[n - 1].get("subsection") or "") for n in cited if 1 <= n <= len(hits)]
    if not any(s.startswith("母公司") for s in subs) or any(s.startswith("合并") for s in subs):
        return {"口径触发": False, "口径缺注": None}
    ok = bool(_CALIBER_NOTE.search(_norm(answer)))
    return {
        "口径触发": True,
        "口径缺注": None if ok else "只引用到母公司口径的片段，回答里没有注明口径",
    }


# ---------------------------------------------------------------- 组装

_INFERENCE_MARK = "据片段数据推断"


def inferential_causation(answer: str) -> dict:
    """拿「据片段数据推断」给原因打掩护的句子（清单）。

    扩展分析的提示词明确禁止把推断标记用于归因，但那只是一串字，模型随时能加上去。
    这条专抓「推断标记 + 因果词」同时出现的句子 —— `causal_sentences` 抓不到它，
    因为那种句子通常带着引用和数字，过得了那道闸。

    这是这个功能最容易漏掉的口子：一句话既有 [n] 又有数字，看起来完全合规，
    但它说的其实是一个片段里没有的因果。
    """
    out = []
    for s in re.split(r"[。；\n]", answer or ""):
        s = s.strip()
        if _INFERENCE_MARK not in s or not _CAUSAL_RE.search(s):
            continue
        if _NEGATION_RE.search(s):
            continue
        out.append(s)
    return {"推断式归因": out, "推断式归因数": len(out)}

def run(answer: str, hits: list[dict], tag: str = "") -> dict:
    """一道题的全部度量。

    键名刻意不用「率」字：出计数和清单，不出百分比。这个语料里已经有过一个
    漂亮的比率（`引用命中率` 对「覆盖」恒为 0.29）被当成质量信号读，而它其实只是排版产物。
    """
    from tools.rubric_questions import anchors

    fid = fidelity_check(answer or "", hits or [])
    return {
        # 1 准确性（片段参照，不是事实正确）
        "金额个数": fid["金额个数"],
        "有出处": fid["有出处"],
        "可推导": fid["可推导"],
        "无出处": fid["无出处"],
        "无出处样例": fid["无出处样例"],
        # 唯一的比率。留着它是因为它的分母（`金额个数`）永远和它印在同一行 ——
        # 见模块开头关于「不要出不带分母的率」的说明。
        "有据可依率": fid["有据可依率"],
        **citations(answer, len(hits or [])),
        **quote_check(answer, hits),
        # 3 逻辑链
        **unit_conversions(answer),
        **computed_rates(answer),
        # 4 证据 / 5 解释层次（只出清单）
        **unsourced_items(answer),
        **causal_sentences(answer),
        # 扩展分析专用：拿"推断"标记给原因打掩护的句子
        **inferential_causation(answer),
        # 2 完整性（锚点下限 + 条件触发的口径标注）
        **anchors(tag, answer),
        **caliber(answer, hits),
    }
