"""⑤ 检索：查询改写 → 多路（向量 + BM25）检索 → RRF（倒数排名融合）合并。"""
import json
import re

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

from . import config
from .indexer import EmbedFn, default_embed

jieba.setLogLevel(60)
for _w in ("营业利润", "营业收入", "营业总收入", "营业成本", "利润总额", "净利润", "归母净利润",
           "扣非净利润", "研发投入", "经营活动", "现金流量", "毛利率", "合并利润表", "母公司利润表",
           "合并资产负债表", "合并现金流量表", "主要会计数据", "主要财务指标", "上年同期", "同比增减"):
    jieba.add_word(_w)

STOPWORDS = {"的", "是", "多少", "什么", "有", "了", "吗", "呢", "请问", "一下", "情况", "如何", "怎么",
             "怎样", "哪些", "公司", "本公司", "该公司", "股份公司", "？", "?", "，", ",", "。", "、",
             "（", "）", "(", ")", "“", "”", "：", ":", " "}

CHANGE_WORDS = ("变化", "变动", "增长", "下降", "增减", "同比", "增幅", "降幅", "提升", "减少", "增加", "趋势")

# ---------- 报表片段的年份表头 ----------
# 症状：命中合并利润表的中段片段时，模型看到的是
#   「财务费用 123,711,547.40 146,963,561.21」
# 两列数字，却没有任何「哪列是哪年」的标注，于是给不出同比。
# 病根：年份表头在同一个 subsection 里**往前数第一个**片段里。实测每张报表只有那一个片段带表头
#   （合并利润表 5 个片段里只有 1 个，合并资产负债表 6 个里只有 1 个）。
# 所以这里不追加相邻片段，而是把表头那几行**前置**到给模型看的文本上：约 20 字，零额外片段、零额外调用，
# 一次修好全部无表头片段。只影响喂给模型的上下文，不改 chunks.json，检索索引不受影响。
_HEADER_RE = re.compile(r"20\d\d\s*年\s*度|单位\s*[:：]\s*元|币种\s*[:：]")
_AMOUNT_RE = re.compile(r"\d[\d,，]*\.\d{2}")
_HEADER_SCAN_MAX = 60      # 往前最多回溯多少个片段
# 表头最多取几行。别设太小：合并资产负债表的年份列标在第 9 行
# （「2025 年 12 月 31 日 2024 年 12 月 31 日」前面还有标题、编制单位、单位、项目、附、注），
# 上限卡在 8 会把最关键的那一行截掉，等于白传播。真正的终止条件是「遇到含金额的行」。
_HEADER_MAX_LINES = 14


def extract_header(text: str) -> str:
    """取片段开头连续的表头行；遇到第一行含金额数字就停（那是数据行，不是表头）。"""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _AMOUNT_RE.search(line):
            break
        lines.append(line)
        if len(lines) >= _HEADER_MAX_LINES:
            break
    return "\n".join(lines)


def needs_header(text: str) -> bool:
    return not _HEADER_RE.search(text[:200])


def tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut_for_search(text.lower()) if t.strip() and t not in STOPWORDS]


def rule_rewrite(query: str, company: str = "", year: str = "") -> str:
    """不调用模型的改写：去掉公司名、补全年份、把“变化”展开成年报用语。"""
    q = query
    for name in filter(None, {company, company[:2] if len(company) >= 4 else ""}):
        q = q.replace(name + "股份公司", "").replace(name + "股份", "").replace(name, "")
    q = re.sub(r"(本|该|这家)?公司", "", q)
    if year:
        y = int(year)
        q = re.sub(r"(?<!\d)(\d{2})年", lambda m: f"20{m.group(1)}年", q)  # “25年”→“2025年”
        q = re.sub(r"今年|本年度|本年|报告期", f"{y}年", q)
        q = re.sub(r"去年|上年度|上一年", f"{y - 1}年", q)
    if any(w in q for w in CHANGE_WORDS):
        q += " 本期比上年同期增减 上年同期"
    return re.sub(r"\s+", " ", q).strip() or query


LLM_REWRITE_PROMPT = """你是上市公司年报检索专家。把用户问题改写成 2-3 条用于在年报中检索的短语，使用年报原文的规范用语。
要求：
- 去掉公司名称；把口语换成会计科目或年报栏目名，例如“赚了多少钱”→“归属于上市公司股东的净利润”。
- 涉及具体财务数字时，加一条带报表名的短语，例如“合并利润表 营业利润”“主要会计数据 营业收入”。
- 涉及变化/增长时，加上“本期比上年同期增减”。
- 报告年度为 {year} 年，“今年/本期”指 {year} 年，“去年/上期”指 {prev} 年。
只输出 JSON：{{"queries": ["...", "..."]}}"""

# 带上历史时追加的规则。要点：让模型自己判断是不是追问，并且**明确禁止**
# 把上一轮的内容硬塞进这一轮的检索词 —— 追问和换话题都用同一段提示词处理。
LLM_REWRITE_FOLLOWUP_RULE = """
另外给了【之前的问题】。先判断现在的问题是哪种：
- 追问（出现「那…呢」「它」「其」「该」「这个」「去年」「同期」这类指代或省略）：
  先把指代补全成一个完整问题，再据此改写检索词。
- 换了话题（现在的问题本身已经说清了主题）：完全忽略【之前的问题】，只按它改写。
无论哪种，都不要把现在的问题里没有提到的内容写进检索词。"""


def llm_rewrite(query: str, year: str = "", client=None, timeout: float = 8.0,
                history: list[str] | None = None, diag: dict | None = None) -> list[str]:
    """用大模型把问题改写成年报术语。失败返回空列表，由调用方退回规则改写。

    history 传最近几轮的**问题**（别传回答：上一轮回答里可能有算错或臆测的数字，
    被带进检索词会污染检索）。带上历史后模型会先消解指代，
    「那去年呢」这种才会变成「2024年 营业收入」。

    diag 传字典时可以拿到改写是成功还是退化了 —— 这个函数吞掉所有异常，
    没有这个信号就没法知道追问功能到底有没有生效（退化成规则改写会静默地
    把「答不出追问」变成「答未找到」）。
    """
    try:
        from .generator import get_client
        client = client or get_client()
        y = int(year) if year else 2025
        system = LLM_REWRITE_PROMPT.format(year=y, prev=y - 1)
        user = query
        if history:
            system += LLM_REWRITE_FOLLOWUP_RULE
            recent = "\n".join(f"- {h}" for h in history[-config.FOLLOWUP_HISTORY_TURNS:])
            user = f"【之前的问题】\n{recent}\n\n【现在的问题】\n{query}"
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0, max_tokens=config.REWRITE_MAX_TOKENS, timeout=timeout,
            response_format={"type": "json_object"},
        )
        choice = resp.choices[0]
        data = json.loads(choice.message.content or "")
        qs = [str(x).strip() for x in data.get("queries", []) if str(x).strip()]
        if diag is not None:
            diag["rewrite"] = "ok" if qs else "empty"
        return qs[:3]
    except json.JSONDecodeError:
        # 走到这里多半是被 max_tokens 截断 —— 这个模型是推理型的，
        # 一次查询改写要烧掉上千个推理 token，上限给小了正文一个字都剩不下，
        # 而 JSON 一截断 json.loads 就抛异常，静默退化成规则改写。
        if diag is not None:
            diag["rewrite"] = "truncated"
        return []
    except Exception as e:
        if diag is not None:
            diag["rewrite"] = "fallback"
            diag["rewrite_error"] = f"{type(e).__name__}: {e}"[:160]
        return []


class Retriever:
    def __init__(self, chunks: list[dict], embeddings: np.ndarray, embed: EmbedFn = default_embed,
                 company: str = "", year: str = "", use_llm_rewrite: bool = False, llm_client=None):
        self.chunks = chunks
        self.emb = embeddings
        self.embed = embed
        self.company, self.year = company, year
        self.use_llm_rewrite = use_llm_rewrite
        self.llm_client = llm_client
        # 优先用带上下文前缀的 index_text；兼容旧索引
        texts = [c.get("index_text") or c["text"] for c in chunks]
        self.bm25 = BM25Okapi([tokenize(t) or ["_"] for t in texts]) if chunks else None
        self.last_queries: list[str] = []

    def search_with_queries(self, query: str, k: int = config.TOP_K, pool: int = 50,
                            rrf_k: int = 60, client=None, use_llm: bool | None = None,
                            history: list[str] | None = None, diag: dict | None = None):
        """检索并同时返回所用的检索词。不修改任何共享状态，多个会话可同时调用。

        client 为本次请求使用的大模型客户端（用于查询改写）；use_llm 覆盖实例默认值。
        history 传最近几轮的问题，用于消解追问里的指代；diag 用来回收改写是否退化。
        """
        if not self.chunks:
            return [], []
        pool = min(pool, len(self.chunks))
        use_llm = self.use_llm_rewrite if use_llm is None else use_llm
        queries = self._expand(query, client, use_llm, history=history, diag=diag)
        qvecs = self.embed([config.QUERY_INSTRUCTION + q for q in queries])

        fused: dict[int, float] = {}
        for q, qv in zip(queries, qvecs):
            vec_rank = np.argsort(-(self.emb @ qv))[:pool]
            bm_scores = self.bm25.get_scores(tokenize(q))
            bm_rank = [i for i in np.argsort(-bm_scores)[:pool] if bm_scores[i] > 0]
            for ranking in (vec_rank, bm_rank):
                for r, i in enumerate(ranking):
                    fused[int(i)] = fused.get(int(i), 0.0) + 1.0 / (rrf_k + r + 1)
        best = self._select_diverse(fused, k)
        hits = [dict(self.chunks[i], score=round(fused[i], 5)) for i in best]
        return self._attach_headers(hits), queries

    def _select_diverse(self, fused: dict[int, float], k: int) -> list[int]:
        """按 RRF 顺序取 k 个，但限制任一 section 的占比。

        语料极度倾斜（实测内置示例年报：第八节财务报告 318 个片段占 67%，
        解释“为什么”的第三节只有 56 个占 12%），纯全局 top-k 会让 8 个位置全被
        财报数字占满，模型手里根本没有解释性材料。

        按 section 而**不是** subsection 限流：合并利润表整张 5 个片段同属一个 subsection，
        按 subsection 限 2 会把模型需要的行切掉。

        配额只约束主循环，回填可以超出配额 —— 宁可变多，也不能因为限流就少给片段。
        最终按分数返回，调用方可以依赖 hits[0] 是最相关的。
        """
        ordered = sorted(fused, key=fused.get, reverse=True)
        picked: list[int] = []
        overflow: list[int] = []
        per_section: dict[str, int] = {}
        for i in ordered:
            section = self.chunks[i].get("section") or ""
            if len(picked) < k and per_section.get(section, 0) < config.MAX_PER_SECTION:
                picked.append(i)
                per_section[section] = per_section.get(section, 0) + 1
            else:
                overflow.append(i)
        for i in overflow:                       # 配额导致取不满时按分数回填
            if len(picked) >= k:
                break
            picked.append(i)
        return sorted(picked, key=fused.get, reverse=True)

    def _attach_headers(self, hits: list[dict]) -> list[dict]:
        """给缺年份表头的报表片段前置表头。只读 self.chunks，不写任何共享状态。"""
        if not config.HEADER_PROPAGATION:
            return hits
        out = []
        for hit in hits:
            hit = dict(hit)
            header = self._find_header(hit)
            if header:
                hit["context_header"] = header
            out.append(hit)
        return out

    def _find_header(self, hit: dict) -> str:
        """往回找同一 subsection 里最近那个带表头的片段，取出它的表头行。"""
        if not needs_header(hit.get("text") or ""):
            return ""                            # 自己就带表头
        idx = hit.get("chunk_id")
        if not isinstance(idx, int) or not (0 <= idx < len(self.chunks)):
            return ""
        if self.chunks[idx].get("chunk_id") != idx:   # chunk_id 必须等于下标，否则放弃
            return ""
        section, subsection = hit.get("section"), hit.get("subsection")
        for j in range(idx - 1, max(-1, idx - 1 - _HEADER_SCAN_MAX), -1):
            c = self.chunks[j]
            # 走出同一个 subsection 就不再往前找，免得把别的报表的表头安上来
            if c.get("section") != section or c.get("subsection") != subsection:
                return ""
            if not needs_header(c.get("text") or ""):
                return extract_header(c.get("text") or "")
        return ""

    def _expand(self, query: str, client, use_llm: bool,
                history: list[str] | None = None, diag: dict | None = None) -> list[str]:
        queries = [rule_rewrite(query, self.company, self.year)]
        if use_llm:
            extra = llm_rewrite(query, self.year, client=client or self.llm_client,
                                history=history, diag=diag)
            if extra and history:
                # 追问场景要丢掉上面那条规则改写。原始问题带指代（「那2024年呢」），
                # 它的词元「那」「年」几乎命中每一个片段，混进 RRF 会把真正相关的片段冲淡。
                # 模型已经做过指代消解，这里只保留它给的检索词。
                # 没有历史时行为完全不变（首轮不传 history）。
                queries = []
            queries += extra
        # 去重并保持顺序
        return list(dict.fromkeys(q for q in queries if q))

    def search(self, query: str, k: int = config.TOP_K, pool: int = 50, rrf_k: int = 60) -> list[dict]:
        """兼容旧调用：只返回片段。检索词保存在 last_queries（单线程使用时才可靠）。"""
        hits, queries = self.search_with_queries(query, k, pool, rrf_k)
        self.last_queries = queries
        return hits
