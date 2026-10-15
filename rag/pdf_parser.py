"""② PDF 文本提取：逐页提取正文，记录页码与所属章节。

使用 pypdfium2（PDFium，Chrome 同款内核）。本机 Smart App Control 会拦截 PyMuPDF 的原生模块。
"""
import re
from collections import Counter
from dataclasses import dataclass, field

import pypdfium2 as pdfium

# A 股年报常见标题：“第一节 重要提示”“第三章 管理层讨论与分析”
HEADING_RE = re.compile(r"^\s*第[一二三四五六七八九十百零\d]+[节章]\s*\S.{0,30}$")
PAGE_NUM_RE = re.compile(r"^\s*[-—\s]*(第\s*)?\d+\s*(页)?\s*([/／]\s*\d+\s*(页)?)?[-—\s]*$")
# 财务报表标题独占一行，如“合并利润表”“母公司现金流量表”
STATEMENT_RE = re.compile(r"^(合并|母公司)(资产负债表|利润表|现金流量表|所有者权益变动表)$")
# 一级小标题“七、 近三年主要会计数据和财务指标”；二级“(一) 主要会计数据”
SUB1_RE = re.compile(r"^[一二三四五六七八九十]{1,3}、\s*\S")
SUB2_RE = re.compile(r"^[(（][一二三四五六七八九十]{1,3}[)）]\s*\S")
SECTION_TITLE_RE = re.compile(r"^\s*第[一二三四五六七八九十百零\d]+[节章]")


@dataclass
class Page:
    page_no: int   # 物理页码，从 1 开始
    section: str   # 所属章节（页首所在章节）
    text: str      # 清洗后的正文
    # 与 text 每一行对应的 (章节, 子标题)，用于切分时保留上下文
    line_ctx: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DocInfo:
    company: str = ""  # 公司简称，如“精达股份”
    year: str = ""     # 报告年度，如“2025”


def open_pdf(path_or_bytes) -> pdfium.PdfDocument:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        return pdfium.PdfDocument(bytes(path_or_bytes))
    return pdfium.PdfDocument(str(path_or_bytes))


def render_page_png(path_or_bytes, page_no: int, scale: float = 1.5) -> bytes:
    """把第 page_no 页（1 起）渲染成 PNG，用于界面核查原页。"""
    import io
    doc = open_pdf(path_or_bytes)
    try:
        img = doc[page_no - 1].render(scale=scale).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


def _repeated_lines(pages_lines: list[list[str]]) -> set[str]:
    """出现在超过一半页面上的行视为页眉页脚。"""
    if len(pages_lines) < 4:
        return set()
    counter = Counter()
    for lines in pages_lines:
        counter.update({ln.strip() for ln in lines if ln.strip()})
    threshold = len(pages_lines) * 0.5
    return {ln for ln, c in counter.items() if c > threshold}


def _toc_sections(doc: pdfium.PdfDocument) -> dict[int, str]:
    """用 PDF 书签生成 {起始页(1起): 章节名}，只取最高层级。"""
    items = []
    for bm in doc.get_toc():
        dest = bm.get_dest()
        idx = dest.get_index() if dest is not None else None
        title = (bm.get_title() or "").strip()
        if idx is not None and title:
            items.append((bm.level, idx + 1, title))
    if not items:
        return {}
    # Word 导出常带 OLE_LINK / _Toc 之类的隐藏书签
    items = [it for it in items if not re.match(r"^_?(OLE_LINK|Toc|Hlk|GoBack)", it[2], re.I)]
    if not items:
        return {}
    top = min(level for level, _, _ in items)
    tops = [it for it in items if it[0] == top]
    # 如果顶层书签里有“第X节”，只保留这类，避免“九、是否存在……”混进章节
    if any(SECTION_TITLE_RE.match(t) for _, _, t in tops):
        tops = [it for it in tops if SECTION_TITLE_RE.match(it[2])]
    result: dict[int, str] = {}
    for _, page, title in tops:
        result.setdefault(page, title)
    return result


def _subheading(line: str) -> tuple[int, str] | None:
    """识别子标题，返回 (层级, 标题)。层级 0 为财务报表标题，会覆盖一、二级。"""
    s = re.sub(r"\s+", " ", line).strip()
    if STATEMENT_RE.match(s.replace(" ", "")):
        return 0, s.replace(" ", "")
    if len(s) > 40 or s.endswith(("。", "；", "：", ":")):
        return None
    # 表格行（“一、营业收入 543,313,191.06 …”）不是标题
    if re.search(r"\d[\d,]*\.\d{2}\b", s):
        return None
    if SUB1_RE.match(s):
        return 1, s
    if SUB2_RE.match(s):
        return 2, s
    return None


_LINE_ITEM_WORDS = ("收入", "成本", "利润", "收益", "损失", "费用", "总额", "净额", "资产", "负债",
                    "权益", "现金", "余额", "每股", "股本", "公积", "变动", "分配", "增加", "减少")


def _looks_like_line_item(title: str) -> bool:
    """财务报表科目行，如“二、营业总成本”“五、其他综合收益的税后净额”。"""
    body = re.sub(r"^[一二三四五六七八九十]{1,3}、\s*", "", title)
    return any(w in body for w in _LINE_ITEM_WORDS) and len(body) <= 20


def extract_doc_info(pages: list[Page]) -> DocInfo:
    """从前几页识别公司简称与报告年度。"""
    head = "\n".join(p.text for p in pages[:3])
    info = DocInfo()
    m = re.search(r"(?:公司|证券|股票)简称[:：]\s*([^\s:：，,]{2,10})", head)
    if m:
        info.company = m.group(1)
    m = re.search(r"(20\d{2})\s*年\s*年度报告", head)
    if m:
        info.year = m.group(1)
    return info


def parse_pdf(path_or_bytes) -> list[Page]:
    doc = open_pdf(path_or_bytes)
    try:
        raw = []
        for i in range(len(doc)):
            tp = doc[i].get_textpage()
            raw.append(re.split(r"\r\n|\r|\n", tp.get_text_bounded()))
            tp.close()
        toc_map = _toc_sections(doc)
    finally:
        doc.close()

    noise = _repeated_lines(raw)
    pages: list[Page] = []
    current = "前言"
    sub1, sub2, statement = "", "", ""
    for idx, lines in enumerate(raw, start=1):
        if toc_map and idx in toc_map:
            if toc_map[idx] != current:
                sub1, sub2, statement = "", "", ""
            current = toc_map[idx]
        page_section = current
        kept, ctx = [], []
        for ln in lines:
            s = ln.strip()
            if not s or s in noise or PAGE_NUM_RE.match(s):
                continue
            # 无书签时用正则识别章节标题（排除目录页的“……”引导线）
            if not toc_map and HEADING_RE.match(s) and "..." not in s and "…" not in s:
                current = re.sub(r"\s+", " ", s)
                sub1, sub2, statement = "", "", ""
                if not kept:
                    page_section = current
            else:
                h = _subheading(s)
                if h:
                    level, title = h
                    if level == 0:
                        statement = title
                    elif statement:
                        # 报表内部的“一、营业收入”“（一）按经营持续性分类”是科目行，不切换上下文。
                        # 报表后面一般是“三、公司基本情况”这类附注章节，只用“一级标题且非科目”退出。
                        if level == 1 and not _looks_like_line_item(title):
                            sub1, sub2, statement = title, "", ""
                    elif level == 1:
                        sub1, sub2 = title, ""
                    else:
                        sub2 = title
            sub = statement or " > ".join(x for x in (sub1, sub2) if x)
            kept.append(s)
            ctx.append((current, sub))
        pages.append(Page(page_no=idx, section=page_section, text="\n".join(kept), line_ctx=ctx))
    return pages
