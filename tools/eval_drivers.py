"""「影响这家公司的几件大事」评测：每家公司按常识必须抓到的关键因素，有没有出现在前 5 个里。

用法：python -m tools.eval_drivers [--tag 名字]　结果写到 eval_out/drivers-<tag>.md（不入库）。要真实模型。

答案表是对公司业务的常识判断（不是程序跑出来的）：例如精达的铜价、牧原的生猪价格、长江电力的来水。
每条写成 (名称, 匹配因素名称的正则, 必须对上的行情代码或 None)。
另外统计：每个因素的引用都能在原文找到（verify 保证）、暴露度保留了几个、说明里有没有被删掉的数字分句。
样本 PDF 不入库，本机没有的跳过。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "eval_out"
S = "data/_samples/"

SAMPLES = {
    "精达股份": ("demo/2d3dee37263a663e/source.pdf", [("铜价", r"铜", "cu")]),
    "时代新材": ("demo/c4b7d146264a84a8/source.pdf",
             [("原材料价格", r"原材料|玻纤|树脂|钢材|橡胶", None), ("风电需求", r"风电", None)]),
    "贵州茅台": ("data/474905deeaf0f875/source.pdf", [("白酒消费需求", r"需求|消费|宏观", None)]),
    "七匹狼": ("data/6da0b63132e3f0f6/source.pdf", [("服装消费需求", r"需求|消费", None)]),
    "万科A": (S + "万科Ａ_2025_FY.pdf", [("房地产销售", r"房地产|销售|楼市", None)]),
    "中国巨石": (S + "中国巨石_2025_FY.pdf",
             [("玻纤售价", r"玻纤.*价|售价|销售价格", None), ("关税贸易", r"关税|贸易|反倾销", None)]),
    "天齐锂业": (S + "天齐锂业_2025_FY.pdf", [("锂价", r"锂.*价", "lc")]),
    "安克创新": (S + "安克创新_2025_FY.pdf", [("关税", r"关税|贸易", None), ("汇率", r"汇率|美元", "USD")]),
    "恒瑞医药": (S + "恒瑞医药_2025_FY.pdf", [("集采医保", r"集采|带量采购|医保", None)]),
    "牧原股份": (S + "牧原股份_2025_FY.pdf",
             [("生猪价格", r"生猪|猪价", "lh"), ("饲料原料", r"饲料|玉米|豆粕|原料", None)]),
    "立讯精密": (S + "立讯精密_2025_FY.pdf", [("消费电子需求", r"消费电子", None), ("大客户", r"客户", None)]),
    "蓝色光标": (S + "蓝色光标_2025_FY.pdf", [("广告投放", r"广告|投放|媒体", None)]),
    "长江电力": (S + "长江电力_2025_FY.pdf", [("来水", r"来水", None), ("利率", r"利率", None)]),
}


def main() -> None:
    from rag import drivers
    from rag.generator import get_client
    from rag.pdf_parser import raw_page_lines

    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=datetime.now().strftime("%m%d-%H%M"))
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    client = get_client()
    have = {k: v for k, v in SAMPLES.items() if (ROOT / v[0]).exists()}
    # pdfium 不能多线程同时解析：先在主线程里依次读完，只把模型调用并发
    pages = {k: raw_page_lines((ROOT / v[0]).read_bytes(), max_pages=10 ** 4) for k, v in have.items()}

    def one(name):
        t = time.time()
        out = drivers.build_from_lines(pages[name], client=client, company=name, year="2025")
        return name, round(time.time() - t, 1), out

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(one, have))

    hit = total = 0
    lines, detail = [], []
    for name, secs, out in results:
        found = (out or {}).get("drivers") or []
        row = []
        for label, pattern, market in have[name][1]:
            total += 1
            match = next((d for d in found if re.search(pattern, d["name"])), None)
            ok = bool(match) and (market is None or match["market"] == market)
            hit += ok
            row.append(("✓ " if ok else "✗ ") + label + ("" if not match or ok else f"（代码 {match['market'] or '空'}）"))
        exposures = sum(1 for d in found if d["exposure"])
        amounts = sum(1 for d in found if (d["exposure"] or {}).get("amount"))
        dropped = ((out or {}).get("stats") or {}).get("exposure_dropped", 0)
        lines.append(f"| {name} | {secs}s | {len(found)}/{(out or {}).get('proposed', 0)} | "
                     f"{exposures}（金额 {amounts}，删 {dropped}） | {'；'.join(row)} |")
        detail.append(f"### {name}\n" + "\n".join(
            f"- [{d['type']}] **{d['name']}** `{d['market'] or '-'}` {d['how']}"
            f"（第 {d['evidence'][0]['page']} 页「{d['evidence'][0]['quote']}」"
            + (f"；暴露：{d['exposure']['text']}，第 {d['exposure']['page']} 页" if d["exposure"] else "") + "）"
            for d in found) if out else f"### {name}\n失败")
    secs = sorted(s for _, s, _ in results)
    head = [f"# 几件大事评测 {args.tag}", "",
            f"**关键因素命中 {hit}/{total} = {hit / total * 100:.0f}%**，样本 {len(results)} 份，"
            f"用时中位数 {secs[len(secs) // 2]} 秒、最长 {secs[-1]} 秒", "",
            "| 公司 | 用时 | 留下/模型提出 | 有暴露度（带金额，核对删掉） | 关键因素 |", "|---|---|---|---|---|"]
    text = "\n".join(head + lines + [""] + detail)
    OUT.mkdir(exist_ok=True)
    (OUT / f"drivers-{args.tag}.md").write_text(text, encoding="utf-8")
    (OUT / f"drivers-{args.tag}.json").write_text(
        json.dumps({n: o for n, _, o in results}, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(head + lines))


if __name__ == "__main__":
    main()
