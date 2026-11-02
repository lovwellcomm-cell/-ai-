"""体检卡抽取准确率：拿人工核对过的答案表，逐份逐项比对 rag/healthcheck.py 的抽取结果。

用法：python -m tools.eval_healthcheck　结果写到 eval_out/healthcheck.md（不入库）。不需要模型。

答案表里的每个数都是对着原文页面人工核过的（页码写在注释里），不是程序跑出来再抄回来。
样本 PDF 不入库：内置示例在 demo/，其余在 data/<doc_id>/ 和 data/_samples/（从巨潮下载）。
本机没有的样本跳过，并在结果里写明。

比的是「抽到的事实」：审计意见/会计调整/质押的灯色、前五名的百分比、商誉和政府补助的金额。
现金流和利润含金量的数来自 financials.py（它自己用披露的增减比例复算核对），不在这里重复。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "eval_out"
S = "data/_samples/"

# 路径: (报告期, {字段: 答案})。百分比字段: cust/supp/cust_rel/supp_rel；金额（元）: goodwill/gov。
ANSWERS: dict[str, tuple[str, dict]] = {
    "demo/2d3dee37263a663e/source.pdf": ("FY", {      # 精达股份 2025
        "audit": "green", "accounting": "green", "pledge": "green",             # p2 / p52 / p65
        "cust": 9.69, "supp": 45.50, "cust_rel": 0.0, "supp_rel": 0.0,          # p17
        "goodwill": 97_443_652.95, "gov": 27_624_254.82}),                      # p72 / p9
    "demo/c4b7d146264a84a8/source.pdf": ("FY", {      # 时代新材 2025
        "audit": "green", "accounting": "green", "pledge": "green",             # p2 / p56 / p77
        "cust": 57.09, "supp": 23.63, "cust_rel": 19.46, "supp_rel": 0.0,       # p16
        "goodwill": 2_608_450.10, "gov": 77_578_100.0}),                        # p84 / p8（万元表）
    "data/474905deeaf0f875/source.pdf": ("FY", {      # 贵州茅台 2025
        "audit": "green", "accounting": "green", "pledge": "green",             # p2 / p38 / p51
        "cust": 10.10, "supp": 38.12, "cust_rel": 3.77, "supp_rel": 15.22,      # p11
        "goodwill": 0.0, "gov": 4_271_947.79}),                                 # p57 空行 / p7
    "data/5299f4940e2ce4e9/source.pdf": ("FY", {      # 贵州茅台 2024
        "audit": "green", "accounting": "green", "pledge": "green",             # p2 / p85 / p54
        "cust": 11.52, "supp": 35.43, "cust_rel": 3.85, "supp_rel": 15.22,      # p10
        "goodwill": 0.0, "gov": 4_539_419.20}),                                 # p59 空行 / p6
    "data/6da0b63132e3f0f6/source.pdf": ("FY", {      # 七匹狼 2025
        "audit": "green", "accounting": "green",                                # p77 / p7
        "cust": 11.58, "supp": 12.39, "cust_rel": 0.0, "supp_rel": 0.0,         # p20–21
        "goodwill": 0.0, "gov": 29_931_597.84}),                                # p82 空行 / p8
    "data/f0fd7be994bcc4b1/source.pdf": ("FY", {      # 七匹狼 2024
        "audit": "green", "accounting": "green",                                # p75 / p7
        "cust": 14.06, "supp": 14.12, "cust_rel": 0.0, "supp_rel": 0.0,         # p19
        "goodwill": 0.0, "gov": 23_902_060.22}),                                # p79 / p8
    S + "万科Ａ_2025_FY.pdf": ("FY", {
        "audit": "red", "accounting": "green",                                  # p2、p146 持续经营 / p10、p100
        "cust": None, "supp": None,                                             # 没有前五名这一节
        "goodwill": 5_703_660_521.23}),                                         # p152
    S + "恒瑞医药_2025_FY.pdf": ("FY", {
        "audit": "green", "accounting": "green", "pledge": "green",             # p2 / p113 / p126
        "cust": 55.72, "supp": 26.61, "cust_rel": 0.0, "supp_rel": 0.0,         # p60
        "goodwill": 0.0, "gov": 404_235_489.95}),                               # p133 无此行、p192 不适用 / p7
    S + "牧原股份_2025_FY.pdf": ("FY", {
        "audit": "green", "accounting": "green", "pledge": "green",             # p106 / p11 / p97
        "cust": 8.28, "supp": 19.48, "cust_rel": 1.24, "supp_rel": 5.61,        # p30
        "goodwill": 0.0, "gov": 151_853_364.01}),                               # p111 空行 / p13
    S + "立讯精密_2025_FY.pdf": ("FY", {
        "audit": "green", "accounting": "green", "pledge": "green",             # p113 / p8 / p97
        "cust": 65.04, "supp": 52.97, "cust_rel": 0.0, "supp_rel": 0.0,         # p19
        "goodwill": 2_239_173_155.52, "gov": 827_502_791.66}),                  # p117 / p9
    S + "蓝色光标_2025_FY.pdf": ("FY", {
        "audit": "green", "accounting": "yellow", "pledge": "green",            # p65 / p7 重述 / p62
        "cust": 13.93, "supp": 91.34, "cust_rel": 0.0, "supp_rel": 0.0,         # p14–15
        "goodwill": 1_759_925_140.48}),                                         # p71（p67 写净额 175,992.51 万元）
    "data/705059e66541f245/source.pdf": ("Q3", {"goodwill": 97_443_652.95}),    # 精达 2026 三季报 p6
    "data/e7c61d0ee9fdf92a/source.pdf": ("Q1", {"goodwill": 97_443_652.95}),    # 精达 2026 一季报 p7
    "data/58b83e17d89ae44d/source.pdf": ("Q3", {"goodwill": 0.0}),              # 七匹狼 2026 三季报 p6 粘连
    "data/329e3ba64f8cb0ec/source.pdf": ("H1", {"goodwill": 0.0}),              # 七匹狼 2026 半年报 p48
}


def actual(path: Path, period: str) -> dict:
    from rag import financials, healthcheck as hc
    from rag.pdf_parser import raw_page_lines

    data = path.read_bytes()
    pages = raw_page_lines(data, max_pages=10 ** 4)
    flat = hc._flat(pages)
    fin = financials.extract(data, period)
    out = {it["key"]: it["status"] for it in hc.build_from_lines(pages, fin, period)["items"]}
    top5 = hc.top5_ratios(flat)
    for k in ("cust", "supp", "cust_rel", "supp_rel"):
        out[k] = top5[k][0] if k in top5 else None
    gw = hc.goodwill(pages)
    out["goodwill"] = gw[0] if gw else "未找到"
    gov = hc._gov_subsidy(pages)
    out["gov"] = round(gov[0], 2) if gov else "未找到"
    return out


def same(want, got) -> bool:
    if isinstance(want, float) and isinstance(got, float):
        return abs(want - got) <= max(0.01, abs(want) * 1e-9)
    return want == got


def main() -> None:
    rows, skipped, total, hits = [], [], 0, 0
    for rel, (period, want) in ANSWERS.items():
        path = ROOT / rel
        if not path.exists():
            skipped.append(rel)
            continue
        got = actual(path, period)
        for field, w in want.items():
            ok = same(w, got.get(field))
            total += 1
            hits += ok
            if not ok:
                rows.append(f"| {rel} | {field} | {w} | {got.get(field)} |")
    rate = hits / total * 100 if total else 0.0
    lines = [f"# 体检卡抽取准确率（{datetime.now():%Y-%m-%d %H:%M}）", "",
             f"**{hits}/{total} = {rate:.1f}%**，样本 {len(ANSWERS) - len(skipped)} 份", ""]
    if rows:
        lines += ["| 样本 | 字段 | 答案 | 抽取 |", "|---|---|---|---|", *rows, ""]
    if skipped:
        lines += ["本机缺少、已跳过：", *[f"- {s}" for s in skipped], ""]
    OUT.mkdir(exist_ok=True)
    (OUT / "healthcheck.md").write_text("\n".join(lines), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
