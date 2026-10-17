"""⑤ 生成：把检索片段编号后交给 DeepSeek，要求逐条标注引用 [n]。"""
import re
from typing import Iterator

from . import config

SYSTEM_PROMPT = """你是一名严谨的上市公司年报研究助手。下面给出的片段都来自同一份年报{doc_desc}。
规则：
1. 只依据【年报片段】回答，不得使用片段以外的知识或臆测数字。片段里没出现公司名不代表不相关，所有片段都属于这份年报。
2. 每个事实和数字后面用 [编号] 标注来源片段，例如：营业收入为 251.2 亿元[2]。
3. 财务数据优先采用合并报表口径（合并利润表、合并资产负债表、主要会计数据）。只找到母公司口径时，要明确注明“母公司口径”。
4. 财务报表行通常是“科目 本期金额 上期金额”。“主要会计数据”表通常是“科目 本年 上年 本期比上年同期增减(%) 前年”。据此区分年份。
5. 问到变化或增长时，给出本期值、上期值、变化额和变化率。片段已给出增减比例的，直接引用；没给的，按片段数字计算，并注明“据片段数字计算”。
6. 金额先换算成“亿元”或“万元”（保留两位小数），再在括号里附原值和单位，例如：8.09 亿元（808,978,679.37 元）。
7. 只有片段里确实没有相关数据时，才回答“年报中未找到相关信息”，并说明找到了哪些相关内容。
8. 使用中文，先给一句话结论，再分点列出依据。"""

# 明确写出字数上限。**必须给数字** —— 实测过几种软约束，都没用：
#     A 不限长度      48.2s  推理 8297 tok  正文 1596 字
#     B 「600 字以内」 17.9s  推理 2845 tok  正文  760 字   ← 快 2.6 倍
#     F 「1000 字以内」33.9s  推理 6111 tok  正文 1449 字
#     G 「别啰嗦」     43.9s  推理 7517 tok  正文 1028 字
#     H 「最多 5 条依据」39.2s 推理 6144 tok  正文  728 字
# G/H 尤其说明问题：H 输出的篇幅和 B 差不多，但它照样想了 6144 个 token。
# 这个模型是推理型的，只有明确的数字预算会让它把任务当成「有界的」来规划，
# 文风类的要求不改变它的思考量。所以别把这条改成「请简洁一些」。
ANSWER_BUDGET_RULE = """
9. 回答要紧凑：先给结论，依据用要点，总长控制在 {budget} 字以内。数字给全，但不要重复啰嗦。"""

# 只用于 A/B：把第二次调用的活并进首轮。见 config.ANSWER_WHY_INLINE 的说明。
ANSWER_WHY_RULE = """
10. 结论之后用 2 句话说明为什么会这样：片段里有原因表述就引原话并标 [编号]；没有就明说“年报未披露具体原因”，不要猜测。"""


def format_context(chunks: list[dict]) -> str:
    parts = []
    for n, c in enumerate(chunks, start=1):
        if c["page_start"] == c["page_end"]:
            pages = f"第{c['page_start']}页"
        else:
            pages = f"第{c['page_start']}-{c['page_end']}页"
        where = c["section"] + (f" · {c['subsection']}" if c.get("subsection") else "")
        body = c["text"]
        # 报表片段常常只有数字没有年份表头（表头在同一个 subsection 往前数第一个片段里），
        # 检索层会把那几行取出来挂在 context_header 上，这里前置上去，模型才分得清哪列是哪年。
        header = c.get("context_header")
        if header:
            body = f"（表头，用来确认各列属于哪一年）\n{header}\n{body}"
        parts.append(f"[{n}]（{where}，{pages}）\n{body}")
    return "\n\n".join(parts)


def _history_block(history: list[str] | None) -> str:
    """最近几轮**问题**拼成的一段。

    只给问题、不给回答：上一轮的回答里可能有算错或臆测的数字，放进提示词会污染这一轮 ——
    检索侧的改写一直就是这么做的（app.py 只把 `t["q"]` 传下去）。
    给问题的用途只有一个：让模型知道「那去年呢」里的「那」指的是什么。
    """
    questions = [q for q in (history or []) if q]
    if not questions:
        return ""
    lines = "\n".join(f"用户：{q}" for q in questions)
    return ("【对话历史】只用来理解当前问题里的指代（如“那”“它”“这个”“去年”），"
            f"不要据此回答，也不要引用这里的任何内容。\n{lines}\n\n")


def build_messages(question: str, chunks: list[dict], company: str = "", year: str = "",
                   history: list[str] | None = None) -> list[dict]:
    desc = f"（{company} {year} 年年度报告）" if company or year else ""
    system = SYSTEM_PROMPT.format(doc_desc=desc)
    if config.ANSWER_CHAR_BUDGET > 0:
        system += ANSWER_BUDGET_RULE.format(budget=config.ANSWER_CHAR_BUDGET)
    if config.ANSWER_WHY_INLINE:
        system += ANSWER_WHY_RULE
    user = (f"【年报片段】\n{format_context(chunks)}\n\n"
            f"{_history_block(history)}"
            f"【问题】\n{question}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def make_client(api_key: str):
    """用给定的 Key 创建 DeepSeek 客户端（OpenAI 兼容接口）。"""
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=config.DEEPSEEK_BASE_URL)


def get_client():
    """使用站长配置的 Key（.env 或 Streamlit Secrets）。"""
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在项目根目录的 .env 中填写。")
    return make_client(config.DEEPSEEK_API_KEY)


def generate(question: str, chunks: list[dict], client=None,
             company: str = "", year: str = "", usage_out: dict | None = None,
             history: list[str] | None = None) -> Iterator[str]:
    """流式返回回答文本片段。

    usage_out 传字典时，会把本次调用的 token 用量填进去（需要 stream_options 才会回传）。
    生成器不能有返回值，所以用这种方式把用量带出来。

    history 是最近几轮的**问题**，用来消解追问里的指代。不给回答。
    """
    client = client or get_client()
    stream = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=build_messages(question, chunks, company, year, history),
        temperature=0.1, stream=True, timeout=config.LLM_TIMEOUT,
        stream_options={"include_usage": True},
    )
    for event in stream:
        usage = getattr(event, "usage", None)
        if usage is not None and usage_out is not None:
            usage_out.update(usage.model_dump() if hasattr(usage, "model_dump") else dict(usage))
        if event.choices and event.choices[0].delta.content:
            yield event.choices[0].delta.content


# ---------- 第二次调用：扩展分析 ----------
# 首轮回答之后，再问一次模型「为什么会是这个结果」，作为独立一段追加。
# 动机是实测发现模型一直在复述检索到的数字，没有做解释 —— 而用户对"深度"的定义正是归因。
#
# 整个提示词的重心是**约束**而不是鼓励发挥。要求模型"必须解释为什么"会把它推向编造：
# 实测「毛利率变动的原因」那道题，年报确实没有单独披露原因，模型当时正确地说了"未披露"。
# 一个逼它给原因的提示词会把这条正确答案变成一个听起来合理、但年报里没有的归因。

ANALYZE_SYSTEM_PROMPT = """你是一名严谨的上市公司年报研究助手。下面给出的是【年报片段】、【已经给出的首轮回答】和【原来的问题】。
首轮回答已经列出了数据和结论。你现在只做一件事：解释为什么会是这个结果。

规则：
1. 只能使用【年报片段】里的内容。不得引入片段之外的任何事实、数字、行业常识或宏观背景。拿不准就少说。
2. 公司自己在片段里说明了原因（例如“主要是……所致”“系……影响”），就引原话并标注 [编号]。
3. 片段里没有原因表述时不要猜测。明确写“年报未披露具体原因”，然后只写能从片段数字直接看出的结构性事实（例如“营业成本增速高于营业收入 0.16 个百分点[1][2]”），并在句末注明“（据片段数据推断）”。“据片段数据推断”只能用来说明结构（占比、差额、增速快慢），**不能用来给原因**。
4. 不要重复首轮回答已经说过的数字和结论，只补充新的解释。
5. 如果首轮回答已经把原因说清楚，或者这个问题本来不涉及原因，只输出三个字：无补充
6. 最多 3 条，总长控制在 {budget} 字以内。
7. 每一条都必须在句末标注 [编号]。标不出来的句子不要写。"""

NO_EXTENSION = "无补充"
EXTENSION_HEADING = "**扩展分析**"
# 首轮回答说「没找到」时**不做**扩展分析：没有结论可解释，此时追问「为什么」只会诱发编造。
# 这个短语由 SYSTEM_PROMPT 第 7 条强制，是固定措辞。
ABSTAIN_MARK = "年报中未找到相关信息"


def build_analyze_messages(question: str, chunks: list[dict], first_answer: str,
                           company: str = "", year: str = "",
                           history: list[str] | None = None) -> list[dict]:
    """第二次调用的消息。复用 format_context，所以 [n] 仍指向**同一批** hits。

    历史同样只给问题：扩展段要解释的是这一轮的问题，而这一轮可能本身就是追问。
    """
    desc = f"（{company} {year} 年年度报告）" if company or year else ""
    system = ANALYZE_SYSTEM_PROMPT.format(budget=config.ANALYZE_CHAR_BUDGET)
    user = (f"【年报片段】{desc}\n{format_context(chunks)}\n\n"
            f"{_history_block(history)}"
            f"【首轮回答】\n{first_answer}\n\n"
            f"【原来的问题】\n{question}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _norm_chars(s: str) -> str:
    """比较重复句时用：去掉空白与各种标点。"""
    return re.sub(r"[\s　*，。；：、,.;:！？!?（）()]+", "", s or "")


def _trim_to_sentence(text: str, limit: int) -> str:
    """超长时在最后一个完整句子边界截断，不留下半句话。"""
    if len(text) <= limit:
        return text
    cut = max((text.rfind(ch, 0, limit) for ch in "。；\n"), default=-1)
    return text[: cut + 1] if cut > 0 else text[:limit]


def clean_extension(first_answer: str, extension: str, n_chunks: int) -> tuple[str, str]:
    """把模型的输出整理成可追加的一段，并给出状态。

    光靠提示词约束不住的事，这里再用运行时拦一道 —— 提示词是请求，代码才是保证：
      · 模型又自己写了一遍小标题 → 去掉，免得多出两层标题
      · 整句和首轮逐字重复 → 丢掉（这是最可能的失败形态：把首轮的话换个说法再说一遍）
      · **一个合法的 [n] 都没有、也没说「未披露」→ 整段丢掉**。
        没有出处就不算解释；这一条是整个功能防编造的最后一道闸。
    """
    t = (extension or "").strip()
    if not t:
        return "", "empty"
    t = re.sub(r"^\s*(?:#{1,6}\s*[^\n]*\n+|\*\*[^*\n]+\*\*\s*\n+)", "", t).strip()
    if not t:
        return "", "empty"
    if t.startswith(NO_EXTENSION):
        return "", "no_add"

    # 逐句去掉和首轮重复的（只认**归一化后完全相同**的句子，不做模糊匹配 ——
    # 模糊匹配会误删本来是新的内容，而这条规则的目的是去水，不是改写）
    first_sents = {_norm_chars(s) for s in re.split(r"[。！？；\n]", first_answer or "")
                   if len(_norm_chars(s)) >= 10}
    kept = [s for s in re.split(r"(?<=[。！？；])", t) if _norm_chars(s) not in first_sents]
    t = "".join(kept).strip()
    if not t:
        return "", "duplicate"

    if not cited_numbers(t, n_chunks) and "未披露" not in t:
        return "", "ungrounded"

    limit = int(config.ANALYZE_CHAR_BUDGET * 1.5)
    if len(t) > limit:
        return _trim_to_sentence(t, limit), "trimmed"
    return t, "ok"


def analyze(question: str, chunks: list[dict], first_answer: str, client=None,
            company: str = "", year: str = "", usage_out: dict | None = None,
            diag: dict | None = None, history: list[str] | None = None) -> str:
    """解释「为什么」。返回扩展段**正文**（不含小标题），没有可补充的返回空串。

    走流式，和 `generate` 保持一致：流式下 `timeout` 是「多久没收到数据」，而不是总时长。
    这个模型是推理型的，一次调用可能思考一分钟以上才吐第一个字；用非流式的话
    `timeout` 就是总时长，会把合法的慢调用误杀掉。同时仍然记录 finish_reason ——
    `max_tokens` 把推理和正文一起算，截断会表现成「正文为空」，
    这正是当年查询改写静默失效的形态，绝不能当成「无补充」。
    """
    diag = diag if diag is not None else {}
    client = client or get_client()
    stream = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=build_analyze_messages(question, chunks, first_answer, company, year,
                                        history),
        temperature=0.1, stream=True, timeout=config.ANALYZE_TIMEOUT,
        max_tokens=config.ANALYZE_MAX_TOKENS,
        stream_options={"include_usage": True},
    )
    parts: list[str] = []
    finish = ""
    for event in stream:
        usage = getattr(event, "usage", None)
        if usage is not None and usage_out is not None:
            usage_out.update(usage.model_dump() if hasattr(usage, "model_dump") else dict(usage))
        if event.choices:
            choice = event.choices[0]
            if getattr(choice, "finish_reason", None):
                finish = choice.finish_reason
            if choice.delta and choice.delta.content:
                parts.append(choice.delta.content)

    diag["finish"] = finish
    raw = "".join(parts)
    text, status = clean_extension(first_answer, raw, len(chunks))
    # 判「截断」看的是**原始输出**，不是清洗后的结果：
    # 正文被当成重复句丢掉也是空，但那不是 max_tokens 的问题，报"截断"会把人带偏。
    if finish == "length" and not raw.strip():
        diag["analyze"] = "truncated"
        raise RuntimeError(
            f"扩展分析被 max_tokens={config.ANALYZE_MAX_TOKENS} 截断，正文为空"
            "（调大 ANALYZE_MAX_TOKENS，或调小 ANALYZE_CHAR_BUDGET）"
        )
    diag["analyze"] = status
    return text


def should_analyze(answer: str) -> bool:
    """这道题要不要做扩展分析。

    弃答时不做 —— 这是这个功能最危险的入口：让模型去解释一个它根本没找到的数据，
    它只能编。毛利率那道题就是这类：片段里确实没有原因表述，而模型当时正确地说了「未披露」。
    """
    return (bool(config.ANALYZE_ENABLED)
            and bool((answer or "").strip())
            and ABSTAIN_MARK not in answer)


def join_answer(first: str, extension: str) -> str:
    """把扩展段接到首轮回答后面，单独成段。没有扩展就原样返回。"""
    if not extension:
        return first
    return f"{first.rstrip()}\n\n{EXTENSION_HEADING}\n{extension.strip()}"


def cited_numbers(answer: str, n_chunks: int) -> list[int]:
    """解析回答中出现的 [n] / [1,2] / [1][3]，返回合法编号（升序）。"""
    nums = set()
    for group in re.findall(r"\[([\d,，、\s]+)\]", answer):
        for x in re.split(r"[,，、\s]+", group):
            if x.isdigit() and 1 <= int(x) <= n_chunks:
                nums.add(int(x))
    return sorted(nums)
