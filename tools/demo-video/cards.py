"""生成片头卡 / 技术卡 / 片尾卡（1440×900 PNG）。

用法：
    python tools/demo-video/cards.py

输出到 tools/demo-video/cards/：
    title.png   片头
    tech.png    技术栈卡
    end.png     片尾
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "cards"
OUT.mkdir(exist_ok=True)

W, H = 1440, 900
BG = (13, 15, 17)
INK = (233, 235, 238)
MUTED = (155, 162, 171)
ACCENT = (224, 178, 78)

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def center_text(d: ImageDraw.ImageDraw, y: int, text: str, f, fill) -> int:
    """居中绘制一行，返回该行底部 y。"""
    box = d.textbbox((0, 0), text, font=f)
    w = box[2] - box[0]
    d.text(((W - w) / 2, y), text, font=f, fill=fill)
    return y + (box[3] - box[1])


def rule(d: ImageDraw.ImageDraw, y: int, width: int = 96, color=ACCENT) -> None:
    d.rectangle([(W - width) / 2, y, (W + width) / 2, y + 3], fill=color)


def title_card() -> None:
    img, d = canvas()
    center_text(d, 250, "年报 AI 研究助手", font(76, bold=True), INK)
    rule(d, 370)
    center_text(d, 410, "把 200+ 页年报，变成可核对的答案", font(34), MUTED)
    center_text(d, 640, "RAG · 混合检索 · DeepSeek · 可追溯到 PDF 原页", font(22), (120, 126, 134))
    img.save(OUT / "title.png")


def tech_card() -> None:
    img, d = canvas()
    center_text(d, 110, "技术实现", font(52, bold=True), INK)
    rule(d, 200)

    lines = [
        ("检索", "bge-small-zh-v1.5（ONNX / CPU）+ BM25 → RRF 融合"),
        ("生成", "DeepSeek，强制 [n] 引用；无据则明说「年报未披露」"),
        ("校验", "385 个金额逐条核对，编造 0 处"),
        ("工程", "167 项自动化测试；单文件 40 MB / 400 页"),
    ]
    y = 290
    for label, body in lines:
        d.text((240, y), label, font=font(30, bold=True), fill=ACCENT)
        d.text((380, y), body, font=font(30), fill=INK)
        y += 110
    img.save(OUT / "tech.png")


def end_card() -> None:
    img, d = canvas()
    center_text(d, 230, "年报 AI 研究助手", font(64, bold=True), INK)
    rule(d, 340)

    rows = [
        ("在线演示", "xlyzp-ai.streamlit.app"),
        ("源码", "github.com/lovwellcomm-cell/-ai-"),
        ("本地运行", "streamlit run app.py"),
    ]
    y = 430
    for label, body in rows:
        d.text((300, y), label, font=font(26), fill=MUTED)
        d.text((470, y), body, font=font(30, bold=True), fill=INK)
        y += 96
    img.save(OUT / "end.png")


if __name__ == "__main__":
    title_card()
    tech_card()
    end_card()
    for p in sorted(OUT.glob("*.png")):
        print(f"{p.name}  {p.stat().st_size // 1024} KB")
