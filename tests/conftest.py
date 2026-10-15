import sys
from pathlib import Path

import numpy as np
import pytest
from fpdf import FPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HEADER = "示例股份有限公司2025年年度报告全文"
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
# 找不到中文字体时，依赖它的测试会被跳过，而不是报错
FONT = next((f for f in _FONT_CANDIDATES if Path(f).exists()), None)
BODIES = [
    ["第一节 重要提示", "本公司董事会保证年度报告内容真实。"],
    ["本报告涉及未来计划等前瞻性陈述，不构成实质承诺。"],
    ["第二节 公司简介和主要财务指标", "公司股票简称为示例股份。"],
    ["2025年营业收入为128.5亿元，同比增长12.3%。"],
    ["归属于上市公司股东的净利润为15.2亿元。"],
    ["研发投入金额为6.8亿元，占营业收入比例5.3%。"],
]


def _need_font():
    if FONT is None:
        pytest.skip("没有找到可用的中文字体，跳过需要生成中文 PDF 的测试")


@pytest.fixture
def sample_pdf() -> bytes:
    """6 页：第1-2页属“第一节”，第3-6页属“第二节”；每页有重复页眉和页码，无书签。"""
    _need_font()
    pdf = FPDF()
    pdf.add_font("hei", fname=FONT)
    for i, lines in enumerate(BODIES, start=1):
        pdf.add_page()
        pdf.set_font("hei", size=9)
        pdf.text(20, 15, HEADER)
        pdf.set_font("hei", size=11)
        y = 40
        for ln in lines:
            pdf.text(20, y, ln)
            y += 10
        pdf.set_font("hei", size=9)
        pdf.text(100, 285, str(i))
    return bytes(pdf.output())


@pytest.fixture
def bookmarked_pdf() -> bytes:
    """带书签的 3 页 PDF，书签标题与正文标题不同，用于验证优先使用书签。"""
    _need_font()
    pdf = FPDF()
    pdf.add_font("hei", fname=FONT)
    pdf.set_font("hei", size=11)
    for title, body in [("书签A", "甲内容"), ("书签B", "乙内容"), (None, "丙内容")]:
        pdf.add_page()
        if title:
            pdf.start_section(title)
        pdf.text(20, 40, body)
    return bytes(pdf.output())


def fake_embed(texts):
    """确定性的字符哈希向量，避免测试下载模型。"""
    vecs = np.zeros((len(texts), 256), np.float32)
    for r, t in enumerate(texts):
        for ch in t:
            vecs[r, hash(ch) % 256] += 1
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-9)
