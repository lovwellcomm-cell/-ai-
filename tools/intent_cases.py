"""「理解问题」测试集：问一句话，系统该切到哪份报告、按什么意图处理、检索词该覆盖什么。

用来给 agent1（规划器）立基线：先用现有流程（识别公司年份 + 规则 + 改写检索词）跑一遍，
记下错在哪；改完再跑同一套题对比。答案由人写死在这里，机器只负责比对。

## 字段

- `cur`：当前停在哪份报告（doc_id，见 DOCS）；`history`：同一段对话里之前问过的问题（旧 → 新）。
- `plan`：可接受的动作，任一命中即算对 ——
  `"current"` / `"multiple"` / `"clarify"` / `("switch", doc_id)` / `("import", 公司, 年份, 期)`。
- `intent`：可接受的意图（任一）：report_qa 答报告 / trend 拉长趋势 / industry 行业动态 /
  clarify 反问 / out_of_scope 超出年报范围。
- `card`：要不要出指标卡；None = 两可，不计分。
- `anchors`：检索词的下限。每组至少一个词出现在检索词里；None = 不计分（不检索的题）。

有的题本身就有两种合理理解（「七匹狼利润率变化」用年报还是最新的半年报），
就把两种都写进可接受的列表 —— 这里考的是「别理解错」，不是「猜中出题人的偏好」。
"""

TODAY = (2026, 11, 2)

# 本地已有的报告（和开发机 data/ 里的一致，demo 是内置示例）
DOCS = [
    {"doc_id": "jd25", "company": "精达股份", "year": "2025", "demo": True},
    {"doc_id": "jd26q1", "company": "精达股份", "year": "2026", "period": "Q1"},
    {"doc_id": "jd26q3", "company": "精达股份", "year": "2026", "period": "Q3"},
    {"doc_id": "sd25", "company": "时代新材", "year": "2025", "demo": True},
    {"doc_id": "mt25", "company": "贵州茅台", "year": "2025"},
    {"doc_id": "mt24", "company": "贵州茅台", "year": "2024"},
    {"doc_id": "qpl25", "company": "七匹狼", "year": "2025"},
    {"doc_id": "qpl24", "company": "七匹狼", "year": "2024"},
    {"doc_id": "qpl26h1", "company": "七匹狼", "year": "2026", "period": "H1"},
]

R, T, I, C, O = "report_qa", "trend", "industry", "clarify", "out_of_scope"

CASES = [
    # ---------------------------------------------------------------- 基础：就在当前报告里答
    dict(id="basic-1", cat="基础", q="营业收入和归母净利润分别是多少？",
         plan=["current"], intent=[R], card=True, anchors=[["营业收入"], ["净利润"]]),
    dict(id="basic-2", cat="基础", q="赚了多少钱",
         plan=["current"], intent=[R], card=True, anchors=[["净利润"]]),
    dict(id="basic-3", cat="基础", q="毛利率变动的原因是什么？",
         plan=["current"], intent=[R], card=True, anchors=[["毛利率"]]),
    dict(id="basic-4", cat="基础", q="前五大客户占比多少",
         plan=["current"], intent=[R], card=False, anchors=[["客户"]]),
    dict(id="basic-5", cat="基础", q="研发投入多少，资本化比例呢",
         plan=["current"], intent=[R], card=False, anchors=[["研发投入"], ["资本化"]]),

    # ---------------------------------------------------------------- 趋势：要拉长到多年
    # 用户实际遇到的：只在当前报告上找「发展」，回答「找不到相关片段」
    dict(id="trend-1", cat="趋势", q="近几年发展如何",
         plan=["current"], intent=[T], card=True,
         anchors=[["营业收入", "主要会计数据"], ["净利润", "主要会计数据"]]),
    dict(id="trend-2", cat="趋势", q="过去五年营收走势",
         plan=["current"], intent=[T], card=True, anchors=[["营业收入"]]),
    dict(id="trend-3", cat="趋势", q="公司这几年成长性怎么样",
         plan=["current"], intent=[T], card=True, anchors=[["营业收入", "主要会计数据"]]),
    dict(id="trend-4", cat="趋势", q="长期来看盈利能力怎么样",
         plan=["current"], intent=[T], card=True, anchors=[["净利润", "毛利率", "净利率"]]),
    dict(id="trend-5", cat="趋势", q="精达近三年净利润",
         plan=["current"], intent=[T, R], card=True, anchors=[["净利润"]]),
    # 用户实际遇到的那一次：停在三季报上问多年，三季报里只有本期和上年同期，
    # 回答「年报中未找到相关信息」。问多年要换到年报（再拉长到 6 年）。
    dict(id="trend-6", cat="趋势", q="近几年发展如何", cur="jd26q3",
         plan=[("switch", "jd25")], intent=[T], card=True,
         anchors=[["营业收入", "主要会计数据"]]),
    dict(id="trend-7", cat="趋势", q="这几年营收增长快吗", cur="jd26q3",
         plan=[("switch", "jd25")], intent=[T], card=True, anchors=[["营业收入"]]),
    dict(id="trend-8", cat="趋势", q="公司发展得怎么样",
         plan=["current"], intent=[T, R], card=True, anchors=[["营业收入", "主要会计数据"]]),
    dict(id="trend-9", cat="趋势", q="利润呢", history=["近几年营收趋势"],
         plan=["current"], intent=[T], card=True, anchors=[["净利润"]]),

    # ---------------------------------------------------------------- 追问：指代和省略
    dict(id="follow-1", cat="追问", q="那2024年呢？", history=["2025年营业收入是多少？"],
         plan=[("import", "精达股份", 2024, "FY")], intent=[R], card=True,
         anchors=[["营业收入"]]),
    dict(id="follow-2", cat="追问", q="净利润呢", history=["营业收入是多少"],
         plan=["current"], intent=[R], card=True, anchors=[["净利润"]]),
    dict(id="follow-3", cat="追问", q="那供应商呢", history=["前五大客户是谁"],
         plan=["current"], intent=[R], card=False, anchors=[["供应商"]]),
    dict(id="follow-4", cat="追问", q="为什么", history=["毛利率怎么变化的"],
         plan=["current"], intent=[R], card=None, anchors=[["毛利率"]]),
    dict(id="follow-5", cat="追问", q="它的现金流呢", history=["七匹狼利润率变化如何"],
         cur="qpl25", plan=["current"], intent=[R], card=True, anchors=[["现金流"]]),

    # ---------------------------------------------------------------- 换公司 / 年份 / 报告期
    dict(id="switch-1", cat="切换", q="七匹狼利润率变化如何？",
         plan=[("switch", "qpl25"), ("switch", "qpl26h1")], intent=[R], card=True,
         anchors=[["净利润", "利润率", "毛利率"]]),
    dict(id="switch-2", cat="切换", q="茅台2024年营收多少",
         plan=[("switch", "mt24")], intent=[R], card=True, anchors=[["营业收入"]]),
    dict(id="switch-3", cat="切换", q="贵州茅台去年年报的净利润",
         plan=[("switch", "mt25")], intent=[R], card=True, anchors=[["净利润"]]),
    dict(id="switch-4", cat="切换", q="精达股份2026年前三季度营收",
         plan=[("switch", "jd26q3")], intent=[R], card=True, anchors=[["营业收入"]]),
    dict(id="switch-5", cat="切换", q="一季度业绩怎么样",
         plan=[("import", "精达股份", 2025, "Q1"), ("switch", "jd26q1")], intent=[R], card=True,
         anchors=[["营业收入", "净利润"]]),
    dict(id="switch-6", cat="切换", q="最新财报如何",
         plan=[("switch", "jd26q3")], intent=[R], card=True, anchors=[["营业收入", "净利润"]]),
    dict(id="switch-7", cat="切换", q="七匹狼最新财报",
         plan=[("import", "七匹狼", 2026, "Q3")], intent=[R], card=True,
         anchors=[["营业收入", "净利润"]]),
    dict(id="switch-8", cat="切换", q="时代新材2025年的毛利率",
         plan=[("switch", "sd25")], intent=[R], card=True, anchors=[["毛利率"]]),
    dict(id="switch-9", cat="切换", q="宁德时代营收多少",
         plan=[("import", "宁德时代", None, "FY")], intent=[R], card=True, anchors=[["营业收入"]]),
    dict(id="switch-10", cat="切换", q="今年前三季度营收",
         plan=[("switch", "jd26q3")], intent=[R], card=True, anchors=[["营业收入"]]),
    dict(id="switch-12", cat="切换", q="去年全年营收多少", cur="jd26q3",
         plan=[("switch", "jd25")], intent=[R], card=True, anchors=[["营业收入"]]),
    dict(id="switch-11", cat="切换", q="Q3单季利润",
         plan=[("import", "精达股份", 2025, "Q3"), ("switch", "jd26q3")], intent=[R], card=True,
         anchors=[["净利润", "利润"]]),

    # ---------------------------------------------------------------- 多家公司
    dict(id="multi-1", cat="多公司", q="茅台和五粮液谁的毛利率高",
         plan=["multiple"], intent=[R], card=None, anchors=None),
    dict(id="multi-2", cat="多公司", q="精达股份和时代新材营收对比",
         plan=["multiple"], intent=[R], card=None, anchors=None),

    # ---------------------------------------------------------------- 行业动态（要联网）
    dict(id="ind-1", cat="行业", q="行业最新动态",
         plan=["current"], intent=[I], card=False, anchors=None),
    dict(id="ind-2", cat="行业", q="竞争对手最近有什么动作",
         plan=["current"], intent=[I], card=False, anchors=None),
    dict(id="ind-4", cat="行业", q="原材料价格最近怎么样",
         plan=["current"], intent=[I], card=False, anchors=None),
    dict(id="ind-3", cat="行业", q="铜价走势对公司有什么影响",
         plan=["current"], intent=[I, R], card=None, anchors=None),

    # ---------------------------------------------------------------- 年报回答不了 / 不该回答
    dict(id="oos-1", cat="超范围", q="现在股价多少",
         plan=["current"], intent=[O, I], card=False, anchors=None),
    dict(id="oos-2", cat="超范围", q="帮我写一首诗",
         plan=["current"], intent=[O], card=False, anchors=None),
    dict(id="oos-3", cat="超范围", q="推荐一只能涨的股票",
         plan=["current"], intent=[O], card=False, anchors=None),
    dict(id="oos-4", cat="超范围", q="今天天气怎么样",
         plan=["current"], intent=[O], card=False, anchors=None),

    # ---------------------------------------------------------------- 太模糊，该反问
    dict(id="vague-1", cat="模糊", q="对比一下",
         plan=["current", "clarify"], intent=[C], card=None, anchors=None),
    dict(id="vague-2", cat="模糊", q="那个数据是多少",
         plan=["current", "clarify"], intent=[C], card=None, anchors=None),

    # ---------------------------------------------------------------- 口语 / 缩写
    dict(id="colloq-1", cat="口语", q="ROE多少",
         plan=["current"], intent=[R], card=True, anchors=[["净资产收益率"]]),
    dict(id="colloq-2", cat="口语", q="现金流咋样",
         plan=["current"], intent=[R], card=True, anchors=[["现金流"]]),
    dict(id="colloq-3", cat="口语", q="负债率高不高",
         plan=["current"], intent=[R], card=True, anchors=[["资产负债率", "负债"]]),
    dict(id="colloq-4", cat="口语", q="分红多少",
         plan=["current"], intent=[R], card=False, anchors=[["分红", "利润分配", "现金红利", "派息"]]),
    dict(id="colloq-5", cat="口语", q="主营业务构成",
         plan=["current"], intent=[R], card=None, anchors=[["主营业务", "分产品", "分行业"]]),
]
