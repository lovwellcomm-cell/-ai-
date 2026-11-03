"""三表速读抽取评测：本机所有报告逐份跑 rag/statements.py，看三张表各自能不能出、对不对。

用法：python -m tools.eval_statements　结果写到 eval_out/statements.md（不入库）。不需要模型。

三层核对：
  · 勾稽自检：每张表程序自己核的那几条（资产 = 负债 + 所有者权益……）。对不上的表不出 —— 这里统计出表率
  · 交叉核对：和「主要会计数据」表（rag/financials.py，另一处原文、另一套抽法）比营收、归母净利润、
    经营现金流、总资产、归母净资产
  · 人工答案：ANSWERS 里的数是对着原文页面人工核过的（页码写在注释里），重点是只有一列有数、
    折行、括号负数这些容易抽错的行
样本 PDF 不入库：内置示例在 demo/，其余在 data/<doc_id>/ 和 data/_samples/（从巨潮下载）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "eval_out"
S = "data/_samples/"

# 路径: {(表, 科目, 列): 答案（元）}；None 表示这一列空着
ANSWERS: dict[str, dict[tuple[str, str, str], float | None]] = {
    "demo/2d3dee37263a663e/source.pdf": {                                       # 精达股份 2025
        ("bs", "cash", "cur"): 2_069_148_661.10,                                # p71
        ("bs", "trading", "cur"): 2_575_806.90, ("bs", "trading", "prev"): None,  # p71 只有本期有数
        ("bs", "bonds", "cur"): None, ("bs", "bonds", "prev"): 264_773_986.12,  # p73 只有上期有数（可转债已转股）
        ("is", "revenue", "cur"): 24_673_670_269.16,                            # p77
    },
    "data/474905deeaf0f875/source.pdf": {                                       # 贵州茅台 2025
        ("bs", "ar_fin", "cur"): 4_457_064_160.16, ("bs", "ar_fin", "prev"): None,  # p57
        ("bs", "notes_rec", "cur"): None, ("bs", "notes_rec", "prev"): 1_984_407_967.50,
        ("bs", "equity_parent", "cur"): 244_637_811_032.18,                     # p58「（或股东权益）合计」折到下一页
    },
    S + "万科Ａ_2025_FY.pdf": {
        ("cf", "op_net", "cur"): -988_124_860.96,                               # p158 括号负数
        ("is", "total_rev", "prev"): 343_176_440_712.96,                        # p155
    },
    S + "中国巨石_2025_FY.pdf": {
        ("bs", "equity", "cur"): 32_550_331_047.47,                             # p86 科目折三行、数单独一行
        ("is", "total_profit", "cur"): 4_149_713_597.69,                        # p89
    },
    S + "立讯精密_2025_FY.pdf": {
        ("cf", "capex", "cur"): 17_904_337_668.17,                              # p124–125 科目被分页隔开
    },
    S + "安克创新_2025_FY.pdf": {
        ("bs", "inventory", "cur"): 4_997_119_037.74,                           # p108 和空着的科目粘成一行
        ("cf", "inv_net", "cur"): 306_007_045.36,                               # p115「投资活动产生/(使用)的」
    },
    "data/ccb04b65494ef2fd/source.pdf": {                                       # 精达 2025 三季报
        ("is", "np_parent", "prev"): 415_852_153.89,                            # p9 两列各占一行
    },
    "data/58b83e17d89ae44d/source.pdf": {                                       # 七匹狼 2026 三季报
        ("bs", "notes_pay", "cur"): 248_627_225.17,                             # p6 整页拍成一行
    },
}

# financials.py 的键 → (表, 科目)
_CROSS = {"revenue": ("is", "revenue"), "np_parent": ("is", "np_parent"), "ocf": ("cf", "op_net"),
          "total_assets": ("bs", "total_assets"), "equity_parent": ("bs", "equity_parent")}


def _reports() -> list[tuple[str, str, str, str]]:
    """(相对路径, 名字, 报告期, 年份)：demo、data 下有 meta 的、样本库。"""
    out = []
    # demo 在前：内置示例在 demo/ 和 data/ 各有一份，按名字去重时留 demo 的（ANSWERS 按它写）
    for meta in sorted((ROOT / "demo").glob("*/meta.json")) + sorted((ROOT / "data").glob("*/meta.json")):
        pdf = meta.parent / "source.pdf"
        if not pdf.exists():
            continue
        m = json.loads(meta.read_text(encoding="utf-8"))
        out.append((pdf.relative_to(ROOT).as_posix(), m.get("name", ""), m.get("period") or "FY",
                    str(m.get("year", ""))))
    for pdf in sorted((ROOT / S).glob("*.pdf")):
        stem = pdf.stem.split("_")
        out.append((pdf.relative_to(ROOT).as_posix(), pdf.name, stem[2] if len(stem) > 2 else "FY",
                    stem[1] if len(stem) > 1 else ""))
    seen, uniq = set(), []
    for r in out:                          # demo 和 data 里可能是同一份
        if r[1] not in seen:
            seen.add(r[1])
            uniq.append(r)
    return uniq


def main() -> None:
    from rag import financials, statements as st

    shown = total_tables = cross_n = cross_ok = ans_n = ans_ok = 0
    table, problems = [], []
    for rel, name, period, year in _reports():
        data = (ROOT / rel).read_bytes()
        out = st.build(data, period)
        cells = []
        for k in ("bs", "is", "cf"):
            total_tables += 1
            shown += out[k]["ok"]
            cells.append("✓" if out[k]["ok"] else "✗ " + out[k].get("why", ""))
        fin = financials.extract(data, period, year)
        for fk, (k, key) in _CROSS.items():
            try:
                want = float(fin["items"][fk]["values"][year])
            except (TypeError, KeyError, ValueError):
                continue
            if not out[k]["ok"]:
                continue
            got = (out[k]["items"].get(key) or {}).get("cur")
            cross_n += 1
            if got is not None and abs(got - want) <= max(1.0, abs(want) * 1e-6):
                cross_ok += 1
            else:
                problems.append(f"| {name} | 交叉核对 {fk} | {want:,.2f} | {got} |")
        for (k, key, col), want in ANSWERS.get(rel, {}).items():
            got = (out[k].get("items") or {}).get(key, {}).get(col)
            ans_n += 1
            if (want is None and got is None) or (want is not None and got is not None and abs(got - want) < 0.01):
                ans_ok += 1
            else:
                problems.append(f"| {name} | {k}.{key}.{col} | {want} | {got} |")
        table.append(f"| {name} | {period} | " + " | ".join(cells) + " |")
    lines = [f"# 三表速读抽取评测（{datetime.now():%Y-%m-%d %H:%M}）", "",
             f"- 出表（勾稽自检通过）：**{shown}/{total_tables}**",
             f"- 和主要会计数据交叉核对：**{cross_ok}/{cross_n}**",
             f"- 人工答案：**{ans_ok}/{ans_n}**", "",
             "| 报告 | 期 | 资产负债表 | 利润表 | 现金流量表 |", "|---|---|---|---|---|", *table, ""]
    if problems:
        lines += ["## 不一致", "", "| 报告 | 项 | 答案 | 抽取 |", "|---|---|---|---|", *problems, ""]
    OUT.mkdir(exist_ok=True)
    (OUT / "statements.md").write_text("\n".join(lines), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
