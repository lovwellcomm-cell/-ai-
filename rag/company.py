"""从问题里识别：用户问的是哪家公司、哪一年。

只负责“理解”，不负责找文件。识别失败或不确定时一律返回空公司，调用方回退到
当前选中的年报 —— 宁可少切一次，也不能把用户的问题带到错误的公司上。
"""
from __future__ import annotations

import json
import re

from . import config

_SUFFIXES = ("股份有限公司", "有限公司", "集团", "控股", "股份", "公司")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})\s*年?")

PROMPT = """你是上市公司年报助手的意图解析器。从用户问题中提取三件事：

1. company：用户**明确提到**的公司名，必须按问题里的写法原样返回。
   - 指代词（它、该公司、这家、其）不算公司名，返回空字符串。
   - 问题里没有公司名，返回空字符串。
2. year：用户明确提到的报告年度，只接受 4 位数字（例如 2024）。
   - “今年 / 去年 / 近三年 / 最新”不要换算，返回 null。
3. multiple：问题里提到两家及以上公司时设为 true，company 填第一家公司名。

只输出 JSON，不要解释：
{"company": "", "year": null, "multiple": false}"""


def normalize(name: str) -> str:
    """去掉常见后缀与空白，用来比较“精达股份”和“精达”。"""
    s = re.sub(r"\s+", "", name or "")
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[:-len(suf)]
    return s


def same_company(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return len(na) >= 2 and len(nb) >= 2 and (na in nb or nb in na)


def year_in(question: str) -> int | None:
    m = _YEAR_RE.search(question or "")
    return int(m.group(1)) if m else None


def _appears(company: str, question: str) -> bool:
    """公司名是否真的出现在问题里；允许“贵州茅台”→“茅台”这种简称。"""
    c, q = normalize(company), normalize(question)
    if not c:
        return False
    if c in q:
        return True
    for n in range(min(4, len(c)), 1, -1):
        if c[-n:] in q:
            return True
    return False


def parse_target(raw: str, question: str = "") -> dict:
    """解析模型输出；任何不合规的字段都退化成空值。"""
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    company = str(data.get("company") or "").strip()[:30]
    if company and not _appears(company, question):
        company = ""                      # 模型臆造的公司名不要
    year = data.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not (1990 <= year <= 2100):
        year = None
    return {"company": company, "year": year,
            "multiple": bool(data.get("multiple"))}


def extract_target(question: str, client=None, current_company: str = "") -> dict:
    """返回 {company, year, multiple}。失败时 company 为空，调用方用当前文档。"""
    q = question or ""
    year = year_in(q)

    # 快路径：问题里就是当前公司，且没有“和/与/对比/比较”这类多公司信号，
    # 不值得再花一次模型调用。
    if current_company and _appears(current_company, q) \
            and not re.search(r"(和|与|对比|比较|相比)", q):
        return {"company": current_company, "year": year, "multiple": False}

    if client is None:
        return {"company": "", "year": year, "multiple": False}
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": q}],
            temperature=0, max_tokens=config.TARGET_MAX_TOKENS,
            timeout=config.TARGET_TIMEOUT,
            response_format={"type": "json_object"},
        )
        out = parse_target(resp.choices[0].message.content, q)
    except Exception:
        out = {"company": "", "year": year, "multiple": False}
    if out["year"] is None:
        out["year"] = year
    return out
