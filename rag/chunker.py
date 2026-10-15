"""③ 文本切分：章节/子标题内按行、句累积成块，带重叠；换章节或子标题时另起一块。"""
import re
from dataclasses import asdict, dataclass

from .pdf_parser import STATEMENT_RE, DocInfo, Page

SENT_SPLIT = re.compile(r"(?<=[。！？；])")


@dataclass
class Chunk:
    chunk_id: int
    text: str          # 原文，用于展示
    section: str
    page_start: int
    page_end: int
    subsection: str = ""
    index_text: str = ""  # 带上下文前缀的文本，用于向量化和 BM25

    def to_dict(self):
        return asdict(self)


def context_prefix(info: DocInfo | None, section: str, subsection: str) -> str:
    parts = []
    if info and (info.company or info.year):
        parts.append(f"{info.company}{info.year}年年度报告".strip())
    parts.append(section)
    if subsection:
        parts.append(subsection)
    return " | ".join(p for p in parts if p)


def _is_statement(sub: str | None) -> bool:
    return bool(sub) and bool(STATEMENT_RE.match(sub))


def _units(pages: list[Page]):
    """把页面拆成 (章节, 子标题, 页码, 文本单元)；长行再按句号拆开。"""
    for p in pages:
        lines = p.text.split("\n") if p.text else []
        ctx = p.line_ctx if len(p.line_ctx) == len(lines) else [(p.section, "")] * len(lines)
        for line, (sec, sub) in zip(lines, ctx):
            for piece in SENT_SPLIT.split(line):
                if piece.strip():
                    yield sec, sub, p.page_no, piece.strip()


def chunk_pages(pages: list[Page], size: int = 500, overlap: int = 80,
                info: DocInfo | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[tuple[int, str]] = []  # (页码, 文本)
    buf_len = 0
    section, subsection = None, None

    def flush(keep_overlap: bool = True):
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n".join(t for _, t in buf)
        chunks.append(Chunk(
            chunk_id=len(chunks),
            text=text,
            section=section,
            subsection=subsection or "",
            page_start=buf[0][0],
            page_end=buf[-1][0],
            index_text=context_prefix(info, section, subsection or "") + "\n" + text,
        ))
        tail, tail_len = [], 0
        if keep_overlap:
            for item in reversed(buf):
                if tail_len + len(item[1]) > overlap:
                    break
                tail.insert(0, item)
                tail_len += len(item[1])
        buf, buf_len = tail, tail_len

    min_len = max(60, size // 6)
    for sec, sub, page_no, text in _units(pages):
        if sec != section or sub != subsection:
            # 换章节或子标题时另起一块，不带重叠，避免表头和上一张表混在一起。
            # 同一章节内，前一块太短（只有一行小标题）时并入下一块，子标题取新的。
            # 但前一块已经是一张财务报表（合并/母公司…表）时必须切开，否则口径会混在一起。
            if sec != section or buf_len >= min_len or _is_statement(subsection):
                flush(keep_overlap=False)
            section, subsection = sec, sub
        # 超长单元硬切
        while len(text) > size:
            buf.append((page_no, text[:size]))
            buf_len += size
            text = text[size - overlap:]
            flush()
        if buf_len + len(text) > size and buf_len > overlap:
            flush()
        buf.append((page_no, text))
        buf_len += len(text)
    flush()
    return chunks
