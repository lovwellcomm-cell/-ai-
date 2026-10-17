"""把 web/{chat.html,chat.css,chat.js} 打包成一份可离线打开的 design/preview.html。

为什么要生成而不是手写：组件文件是唯一真相。手写一份预览稿迟早会和线上实现分叉，
届时「设计稿」就不再代表真实界面了。这个脚本把三个文件内联进一个自包含页面，
再用一个假的 component 对象（模拟 Streamlit 注入的 ShadowRoot 与触发器）驱动真实代码。

用法：
    python tools/build_preview.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUT = ROOT / "design" / "preview.html"

DOC = {"company": "精达股份", "year": "2025", "name": "精达股份2025年报.pdf",
       "pages": 246, "chunks": 471}

SOURCES = [
    {"n": 1, "section": "第三节 管理层讨论与分析", "sub": "主营业务分析 · 分产品情况", "p1": 24, "p2": 25,
     "cited": True,
     "text": "报告期内，公司实现营业收入 23,680,421,173.50 元，较上年同期增长 12.39%；营业成本 "
             "20,909,148,362.41 元。其中境外收入 4,309,836,653.52 元，占营业收入的 18.20%。"},
    {"n": 2, "section": "第三节 管理层讨论与分析", "sub": "主要会计数据和财务指标", "p1": 18, "p2": 18,
     "cited": True,
     "text": "归属于上市公司股东的净利润 642,103,886.44 元，较上年同期增长 18.67%；归属于上市公司"
             "股东的扣除非经常性损益的净利润 598,442,110.07 元。"},
    {"n": 3, "section": "第十节 财务报告", "sub": "合并利润表", "p1": 96, "p2": 96, "cited": True,
     "text": "营业收入 23,680,421,173.50 ／ 营业成本 20,909,148,362.41 ／ 营业利润 812,664,905.18 ／ "
             "利润总额 806,331,472.66 ／ 净利润 690,558,204.31。母公司营业收入 8,814,220,769.14 元。"},
    {"n": 4, "section": "第三节 管理层讨论与分析", "sub": "毛利率变动分析", "p1": 31, "p2": 31, "cited": False,
     "text": "主营业务毛利率 11.70%，较上年同期增加 0.60 个百分点，主要系特种导体及新能源汽车用扁线"
             "产品收入占比提升，以及铜价传导机制下加工费保持稳定所致。"},
]

RICH = {
    "role": "ai", "status": "done",
    "text": ("报告期内，公司实现营业收入 **236.80 亿元**，同比增长 **12.4%**[1]；归属于上市公司股东的"
             "净利润 **6.42 亿元**，同比增长 **18.7%**[1][2]。利润增速快于收入增速，主要来自产品结构"
             "改善而非规模扩张。\n\n"
             "收入按合并报表口径列示，母公司营业收入 88.14 亿元，占合并口径的 37.2%[3]。以下数据均"
             "取自合并报表。\n\n"
             "分产品看，漆包线仍是收入主体，特种导体增速最快；分地区看，境外收入占比提升至 "
             "**18.2%**[1]。"),
    "queries": ["2025年 合并利润表 营业收入 归母净利润", "主要会计数据和财务指标 本期比上年同期增减",
                "主营业务收入构成 分产品"],
    "hits": 8,
    "sources": SOURCES,
    "demo": True,
    "kpis": [
        {"k": "营业收入", "v": "236.80", "u": "亿元", "d": "12.4%", "up": True},
        {"k": "归母净利润", "v": "6.42", "u": "亿元", "d": "18.7%", "up": True},
        {"k": "综合毛利率", "v": "11.70", "u": "%", "d": "0.60pct", "up": True},
    ],
    "charts": [
        {"title": "营业收入", "range": "2022–2025", "unit": "亿元",
         "labels": ["2022", "2023", "2024", "2025"], "values": [178.4, 196.2, 210.7, 236.8],
         "yoy": ["—", "+10.0%", "+7.4%", "+12.4%"]},
        {"title": "归母净利润", "range": "2022–2025", "unit": "亿元",
         "labels": ["2022", "2023", "2024", "2025"], "values": [3.96, 4.31, 5.41, 6.42],
         "yoy": ["—", "+8.8%", "+25.5%", "+18.7%"]},
    ],
    "table": {
        "head": ["产品", "营业收入", "占比", "同比"],
        "rows": [
            {"name": "漆包线", "v": 138.6, "share": 58.5, "yoy": "+8.2%", "up": True},
            {"name": "特种导体", "v": 52.4, "share": 22.1, "yoy": "+24.6%", "up": True},
            {"name": "汽车扁线", "v": 31.7, "share": 13.4, "yoy": "+31.2%", "up": True},
            {"name": "其他", "v": 14.1, "share": 6.0, "yoy": "-3.4%", "up": False},
        ],
    },
}

# 扩展分析进行中的一帧：首轮回答已经画出来了，下面挂着「正在扩展分析」的指示。
# 这是两段式呈现最容易做坏的地方（一返回 spinner 就把正文丢掉），所以放进预览里，
# 每次改前端都能对着看一眼。
ANALYZING = dict(RICH, status="analyzing", statusText="正在扩展分析…",
                 text=RICH["text"].split("\n\n")[0])

ANSWERS = {
    "营业收入和归母净利润分别是多少？": RICH,
    "毛利率变动的原因是什么？": ANALYZING,
}
DEFAULT_ANSWER = dict(RICH, text=(
    "（离线预览只会播放固定的示例回答，真实系统里这一条由 DeepSeek 依据检索到的年报片段生成。）\n\n"
    "报告期内，公司实现营业收入 **236.80 亿元**，同比增长 **12.4%**[1]；归属于上市公司股东的净利润 "
    "**6.42 亿元**，同比增长 **18.7%**[1][2]。"), demo=True)

BASE = {
    "theme": "light", "busy": False, "warn": "", "hint": "",
    "model": "deepseek-flash", "embedModel": "bge-small-zh-v1.5",
    "cardTitle": "主要会计数据", "cardNote": "演示数据 · 非年报原文",
    "heroEyebrow": "精达股份 2025 年度报告 · 已建索引",
    "heroFoot": "246 页全文已切分为 471 个片段，章节、页码与报表名一并保留；提问只检索当前选中的这一份年报。",
    "placeholder": "就《精达股份2025年报.pdf》提问，例如：营业收入和归母净利润分别是多少？",
    "placeholderShort": "就当前年报提问，例如：营业收入是多少？",
    "doc": DOC,
    "messages": [],
}

BOOTSTRAP = """
const HTML = %(html)s;
const CSS = %(css)s;
const BASE = %(base)s;
const ANSWERS = %(answers)s;
const FALLBACK = %(fallback)s;

const host = document.getElementById('host');
const sr = host.attachShadow({ mode: 'open' });
sr.innerHTML = '<style>' + CSS + '</style>' + HTML;

let data = Object.assign({}, BASE);

// 模拟 Streamlit 注入的组件对象：parentElement 就是 ShadowRoot，
// setTriggerValue / setStateValue 换成直接重渲染。
const api = {
  parentElement: sr,
  key: 'preview',
  name: 'annual_report_chat',
  data: data,
  setStateValue: function (k, v) {
    if (k === 'theme') { data = Object.assign({}, data, { theme: v }); remount(); }
  },
  setTriggerValue: function (k, v) {
    if (k === 'ask') {
      const q = (v && v.text) || '';
      const answer = ANSWERS[q] || FALLBACK;
      data = Object.assign({}, data, {
        messages: [{ role: 'user', text: q }, answer],
      });
      remount();
    }
    // open_page：离线预览没有 PDF 可渲染，保持原状
  },
};
function remount() { api.data = data; mount(api); }
mount(api);
"""


def main() -> None:
    html = (WEB / "chat.html").read_text(encoding="utf-8")
    css = (WEB / "chat.css").read_text(encoding="utf-8")
    js = (WEB / "chat.js").read_text(encoding="utf-8")

    if "export default function (component) {" not in js:
        raise SystemExit("chat.js 的入口签名变了，preview 构建脚本需要同步更新")
    js = js.replace("export default function (component) {", "function mount(component) {", 1)

    bootstrap = BOOTSTRAP % {
        "html": json.dumps(html, ensure_ascii=False),
        "css": json.dumps(css, ensure_ascii=False),
        "base": json.dumps(BASE, ensure_ascii=False),
        "answers": json.dumps(ANSWERS, ensure_ascii=False),
        "fallback": json.dumps(DEFAULT_ANSWER, ensure_ascii=False),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        "<title>年报 AI 研究助手 · 界面预览</title>\n"
        "<style>html,body{height:100%;margin:0}#host{display:block;height:100%}</style>\n"
        "</head>\n<body>\n<div id=\"host\"></div>\n<script type=\"module\">\n"
        + js + "\n" + bootstrap + "\n</script>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    print(f"已生成 {OUT.relative_to(ROOT)}（{OUT.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
