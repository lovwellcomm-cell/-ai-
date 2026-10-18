# 年报 AI 研究助手

上传上市公司年报 PDF，用自然语言提问，得到带**原文引用和页码**的回答。每个论断后都标有 `[n]`，点开可以看到原文片段，还能一键查看 PDF 原页，方便核对。

**[在线演示](https://xlyzp-ai.streamlit.app/)** · **[源码](https://github.com/lovwellcomm-cell/-ai-)** · **[演示视频（66 秒）](demo/demo-90s.mp4)**

> 演示覆盖：可追溯引用（点击 `[n]` 打开右侧原文抽屉）· PDF 原页核对 · 同题追问 · 文档切换 · 自定义 API Key · 单文件 40 MB / 400 页上限。
> 视频为真实录屏，LLM 等待段已标注并加速。

## 它怎么工作

```
PDF ─► 提取文本 ─► 切分 ─► 建索引 ─► 检索 ─► 大模型生成 ─► 回答 + 引用 + 页码
      (页码/章节)  (保留    (向量 +    (查询改写  (DeepSeek,        │
                   报表上下文) 关键词)   + 混合检索) 强制标注 [n])   ▼
                                                              扩展分析（解释为什么）
```

- **提取**：用 pypdfium2 逐页提取，记录页码。章节优先读 PDF 书签，没有书签就识别“第X节”标题。自动去掉页眉页脚。
- **切分**：约 500 字一块，章节内重叠 80 字，不跨章节。能识别“合并利润表”“母公司资产负债表”等报表标题，合并报表和母公司报表分开存放。
- **索引**：每个片段前加上“公司、年度、章节、报表名”，再用 bge-small-zh-v1.5（ONNX Runtime，CPU 运行）生成向量，同时建立 BM25 关键词索引。
- **检索**：先把口语问题改写成年报用语，比如“25年营业利润”改成“2025年 合并利润表 营业利润”。向量检索和关键词检索各查一遍，再用 RRF 合并排序。
- **生成**：只依据检索到的片段回答，数字附原值，优先采用合并报表口径。片段里没有相关信息时，明确说“年报中未找到”，不编造。
- **扩展分析**：首轮回答一出来就先显示，然后再问一次模型「为什么会是这个结果」，作为独立一段追加在后面
  （用户不必等第二次调用）。它只能用**同一批片段**：公司写了原因就引原话，没写就明说「年报未披露具体原因」，
  从数字推出的结构性判断必须标注「据片段数据推断」且不能用来给原因；没有可补充的内容时不追加。
- **外部补充（可选）**：首轮明确说「年报中未找到」时，允许模型通过 Tool Calls 调用 DeepSeek 的
  Search / Fetch 接口补充公开信息，或用内置计算器做确定性计算。补充段单独成段，标题写明
  「未经年报核验」，每个外部事实必须标 `[网n]`，没有出处的整段丢弃；**年报里答得出的问题不会触发**。
  `WEB_SEARCH_ENABLED=0` 可关闭。
- **粘贴巨潮链接**：在侧边栏粘贴巨潮资讯（cninfo）的 PDF 地址，由服务器下载并校验后直接建索引，
  不需要先存到本地。只接受 `www.cninfo.com.cn` / `static.cninfo.com.cn` 的 https 链接，
  限 40 MB、30 秒超时；下载失败只报错，不影响已有文档。`REPORT_FETCH_ENABLED=0` 可关闭。
- **提问即自动找年报**：问题里提到别的公司或别的年份时（例如当前是精达、问“七匹狼利润率变化如何”），
  自动去巨潮找对应年报、下载建索引、切换文档再回答，答案上方标注「已自动导入《…》」。
  本地已有该年报时只切换、不重复下载；没提公司时用当前文档；找不到时明确报错，绝不拿当前文档硬答。
  `AUTO_IMPORT_ENABLED=0` 可关闭。

## 本地运行

```bash
pip install -r requirements.txt
cp .env.example .env      # 填入 DEEPSEEK_API_KEY
streamlit run app.py
```

国内网络下载向量模型较慢时，在 `.env` 里加一行 `HF_ENDPOINT=https://hf-mirror.com`。首次提问前会下载约 95 MB 的模型。

运行测试：

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 界面结构

界面分两块，改哪一块、怎么改，差别很大：

| 区域 | 实现 | 说明 |
|---|---|---|
| 左侧文档栏 | 原生 Streamlit 控件 | 上传、文档列表、自己的 Key。**上传必须留在这里** —— 40MB 的 PDF 走组件桥会在每次 rerun 被重新序列化 |
| 主区对话面板 | `st.components.v2` 自定义组件 | 首页、消息、引用抽屉、原页预览、输入框，全部由 `web/` 下的文件渲染 |

```
web/chat.html   组件静态骨架（Streamlit 作为 html= 参数注入）
web/chat.css    全部样式（设计令牌挂在 .arb 上，不是 :root —— shadow root 里 :root 匹配不到）
web/chat.js     渲染与交互（ES module，export default function(component)）
ui/payload.py   把会话状态整形为组件需要的 data（纯函数，可单测）
ui/chat_component.py  注册并挂载组件
```

外壳（隐藏 Streamlit 顶栏、清零留白、侧边栏配色）由 `app.py` 里的 `shell_style()` 注入。

**改 `web/` 下的文件后刷新浏览器即可**，不用重启 streamlit —— `ui/chat_component.py` 会在每次
rerun 比对文件 mtime，变了就重新注册组件。生产环境进程只启动一次，等于只注册一次。
（已在 `streamlit==1.66.0` 上验证；`curl` 或浏览器强刷都能拿到新版本。）

**改 `rag/`、`ui/` 下的 Python 文件，线上不一定马上生效**：Cloud 原地更新时不一定重启进程，
`app.py` 每次 rerun 都从磁盘重读，但已经导入的 `rag.*` / `ui.*` 会留在 `sys.modules` 里。
`rag/freshness.py` 在每次脚本运行的最开头比对一次源码内容戳，发现磁盘变了就把整个
`rag`/`ui` 从 `sys.modules` 摘掉，让随后的 import 重新执行 —— 所以部署后会自己换到新代码，
不需要再手工改一个文件触发部署。代价：重载会清掉检索器缓存（下次提问重新加载索引，
几百毫秒），并让下次建索引重新加载 ONNX 会话（会话本身放在 `builtins` 里，不会重复占内存）。
每日额度不随重载清零，这是 `get_quota()` 刻意不带戳的原因。

预览稿：

```bash
python tools/build_preview.py    # 由 web/ 三个文件生成 design/preview.html
```

`design/preview.html` 是**生成物**，不要直接编辑 —— 它是把 `web/` 的三个文件内联、再用一个假的
component 对象驱动同一份代码，所以永远和线上实现保持一致。用它可以在不起后端、不花 API 额度的情况下
看完整界面。

### 两条容易踩的坑

- **组件里的 HTML 不会被消毒**，而 `data` 里带着用户提问和模型输出。所以 `web/chat.js` 里所有动态文本
  一律走 `textContent` / `createTextNode`，绝不能拼进 `innerHTML`。
- **`data` 里混进 set / Path / numpy 标量会让整个 payload 静默降级成一段 Python repr 字符串**，
  前端只看到 `undefined` 且不报错。`ui/payload.py:json_safe()` 会在挂载前自检并抛出，别绕过它。

## 部署到 Streamlit Community Cloud

1. 把仓库推到 GitHub。
2. 在 share.streamlit.io 选 New app，选择仓库、分支 `main`、入口文件 `app.py`。
3. 在 Advanced settings 里选 Python 3.12 或 3.13，Secrets 填：

```toml
DEEPSEEK_API_KEY = "sk-..."
PUBLIC_DEMO = "1"
```

公开演示模式（`PUBLIC_DEMO=1`）会开启保护：

| 保护 | 默认值 | 说明 |
|---|---|---|
| 每个会话的免费提问 | 5 次 | 用完后提示填写自己的 Key |
| 全站每天的免费提问 | 100 次 | 按天清零 |
| 访客自带 Key | 可选 | 填写后不计入免费额度，Key 只保存在当前会话内存里 |
| 上传限制 | 400 页、40 MB、每会话 2 份 | |
| 上传隔离 | 开启 | 访客只能看到示例年报和自己上传的文件 |
| 自动清理 | 24 小时 | 访客上传的文件会被定期删除 |

这些限制是软限制：新开浏览器会话能重置会话计数，应用重启会清零全站计数。真正的损失上限是 DeepSeek 账户里预充值的余额，建议只充少量。

### 部署后确认线上跑的是哪一版

推送后打开 `https://<你的应用>/?build=1`，侧边栏会出现「构建自检」面板：

| 字段 | 看什么 |
|---|---|
| `source_stamp` / `imported_stamp` | 磁盘源码与进程内模块的指纹，相等才说明进程跑的是最新代码 |
| `process_started` | 这个进程的启动时间。早于本次推送 → 你连的是部署前的旧进程，刷新或新开标签页即可 |
| `reload_count` / `reloaded_at` | 进程跨部署存活时，守卫卸载重导模块的次数与时间 |
| `verdict` | 上面三条的结论，一句话 |

本地对账：`python -c "from rag.freshness import source_stamp; print(source_stamp())"`，与页面上的
`source_stamp` 一致即可。

## 配置项

在 `.env` 或 Streamlit Secrets 中设置，见 [.env.example](.env.example)。

## 已知限制

- **回答的深度没有被自动度量。** [`docs/depth-standard.md`](docs/depth-standard.md) 里那套检查
  能抓编造来源、算错账、无据归因 —— 也是回归护栏，但**分辨不出"更深"和"更浅"**。
  而且它测的是「没超出检索到的片段」，不是「事实正确」：年报本身有错、或模型把年报读反了，它抓不到。
- 只支持文字版 PDF，不支持扫描件（没有 OCR）。
- 每次只检索一份年报，不支持跨公司对比。
- Streamlit 免费托管的服务器在美国，国内访问速度和稳定性取决于网络。应用闲置约 12 小时后会休眠，再次打开需要几十秒唤醒，第一次提问还要重新下载模型。
- 访客上传的文件不保证长期保存。
- 本项目未使用 PyMuPDF 和 PyTorch，因为部分 Windows 环境的应用控制策略会拦截它们的原生库。

## 许可

代码使用 MIT 许可证，见 [LICENSE](LICENSE)。`demo/` 中的示例年报来自上市公司公开披露的文件，版权归原公司所有，仅用于演示。
