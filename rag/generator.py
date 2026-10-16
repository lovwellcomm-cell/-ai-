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


def build_messages(question: str, chunks: list[dict], company: str = "", year: str = "") -> list[dict]:
    desc = f"（{company} {year} 年年度报告）" if company or year else ""
    system = SYSTEM_PROMPT.format(doc_desc=desc)
    user = f"【年报片段】\n{format_context(chunks)}\n\n【问题】\n{question}"
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
             company: str = "", year: str = "", usage_out: dict | None = None) -> Iterator[str]:
    """流式返回回答文本片段。

    usage_out 传字典时，会把本次调用的 token 用量填进去（需要 stream_options 才会回传）。
    生成器不能有返回值，所以用这种方式把用量带出来。
    """
    client = client or get_client()
    stream = client.chat.completions.create(
        model=config.LLM_MODEL, messages=build_messages(question, chunks, company, year),
        temperature=0.1, stream=True, timeout=config.LLM_TIMEOUT,
        stream_options={"include_usage": True},
    )
    for event in stream:
        usage = getattr(event, "usage", None)
        if usage is not None and usage_out is not None:
            usage_out.update(usage.model_dump() if hasattr(usage, "model_dump") else dict(usage))
        if event.choices and event.choices[0].delta.content:
            yield event.choices[0].delta.content


def cited_numbers(answer: str, n_chunks: int) -> list[int]:
    """解析回答中出现的 [n] / [1,2] / [1][3]，返回合法编号（升序）。"""
    nums = set()
    for group in re.findall(r"\[([\d,，、\s]+)\]", answer):
        for x in re.split(r"[,，、\s]+", group):
            if x.isdigit() and 1 <= int(x) <= n_chunks:
                nums.add(int(x))
    return sorted(nums)
