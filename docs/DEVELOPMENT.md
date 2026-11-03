# 开发说明

给要改这个项目的人看：界面怎么搭的、改了代码怎么生效、部署后怎么确认、踩过哪些坑。
产品层面的说明见 [README](../README.md) 和 [产品文档](PRODUCT.md)。

## 界面结构

界面分两块，改哪一块、怎么改，差别很大：

| 区域 | 实现 | 说明 |
|---|---|---|
| 左侧文档栏 | 原生 Streamlit 控件 | 上传、文档列表、自己的 Key。**上传必须留在这里** —— 40 MB 的 PDF 走组件桥，每次 rerun 都会被重新序列化 |
| 主区对话面板 | `st.components.v2` 自定义组件 | 首页、消息、卡片、引用抽屉、原页预览、输入框，全部由 `web/chat.*` 渲染 |
| 开屏页 | 另一个自定义组件 | `web/landing.*`，不加载检索器和模型 |

```
web/chat.html          组件静态骨架（作为 html= 参数注入）
web/chat.css           全部样式（设计令牌挂在 .arb 上，不是 :root —— shadow root 里 :root 匹配不到）
web/chat.js            渲染与交互（ES module，export default function(component)）
web/landing.*          开屏页
ui/payload.py          把会话状态整形成组件要的 data（纯函数，可单测）
ui/chat_component.py   注册并挂载对话组件；ui/landing_component.py 同理
ui/turn.py             回合状态机的纯核心，app.py 只负责接 session_state 和真实模型调用
```

外壳（隐藏 Streamlit 顶栏、清零留白、侧边栏配色）由 `app.py` 的 `shell_style()` 注入。

## 改了代码怎么生效

**`web/` 下的文件**：刷新浏览器即可，不用重启 streamlit。`ui/chat_component.py` 每次 rerun 比对文件 mtime，
变了就重新注册组件；生产环境进程只启动一次，等于只注册一次。

**`rag/`、`ui/` 下的 Python 文件**：本地重启 streamlit。线上 Streamlit Community Cloud 原地更新时不一定重启进程，
已经导入的 `rag.*` / `ui.*` 会留在 `sys.modules` 里。`rag/freshness.py` 在每次脚本运行的最开头比对一次
源码内容戳，发现磁盘变了就把整个 `rag` / `ui` 从 `sys.modules` 摘掉，让随后的 import 重新执行，
所以部署后会自己换到新代码。

- 用内容戳而不是 mtime：部署工具可能保留时间戳。
- 整体摘掉而不是 `importlib.reload`：`from x import y` 留下的绑定 reload 更新不到，逐个 reload 还要自己排依赖顺序。
- 代价：重载会清掉检索器缓存（下次提问重新加载索引，几百毫秒）。ONNX 会话放在 `builtins` 里，不会重复占内存。
- 每日免费额度不随重载清零：`get_quota()` 刻意不带源码戳。
- `st.cache_*` 的缓存破坏参数不要用 `_` 开头（Streamlit 会忽略这种参数，缓存就不会失效）。

## 部署后确认线上跑的是哪一版

推送后打开 `https://<你的应用>/?build=1`，侧边栏出现「构建自检」面板：

| 字段 | 看什么 |
|---|---|
| `source_stamp` / `imported_stamp` | 磁盘源码与进程内模块的指纹，相等才说明进程跑的是最新代码 |
| `process_started` | 进程启动时间。早于本次推送 → 连的是部署前的旧进程，刷新或新开标签页 |
| `reload_count` / `reloaded_at` | 进程跨部署存活时，守卫卸载重导模块的次数与时间 |
| `verdict` | 上面三条的结论 |

本地对账：`python -c "from rag.freshness import source_stamp; print(source_stamp())"`，与页面上的 `source_stamp` 一致即可。
只改 `web/` 时指纹不变，直接看线上页面。Cloud 拉取新代码通常要两三分钟。

`/~/+/` 子路径（如 `/~/+/?build=1`）直接打开应用本体，不经过 Cloud 的外层页面，自动化测试用它；本地没有这个路径。

## 预览，不起后端

```bash
python tools/build_preview.py            # 由 web/chat.* 生成 design/preview.html
python -m tools.build_landing_preview    # 开屏页预览
```

`design/preview.html` 是**生成物**，不要直接编辑：它把 `web/` 的文件内联，再用一个假的 component 对象驱动同一份代码，
所以和线上实现一致。不起后端、不花 API 额度就能看完整界面。

## 必须遵守

- **组件里的 HTML 不消毒**，而 `data` 里有用户提问和模型输出。`web/chat.js` 里所有动态文本一律走
  `textContent` / `createTextNode`；只有内部常量和用 `esc()` 拼的 SVG 可以走 `innerHTML`。
- **浏览器存档是访客可控输入**：刷新后组件把 localStorage 里的对话送回服务器，`ui/memory.py` 只收白名单字段、
  校验类型和长度，文档 id 必须是服务器上真有的。卡片这类结构复杂的数据有总大小上限（`_OPAQUE_MAX_BY_KEY`），
  新卡片超了会在刷新后悄悄消失。
- **`data` 里混进 set / Path / numpy 标量**会让整个 payload 静默降级成一段 Python repr 字符串，前端只看到 `undefined`、
  不报错。`ui/payload.py` 的 `json_safe()` 在挂载前自检并抛出，别绕过它。
- **下载只认巨潮白名单**（`www.cninfo.com.cn`、`static.cninfo.com.cn`），只走 https，拒绝内网地址（防 SSRF）。
- **`.env`、`.streamlit/secrets.toml`、`data/` 永远不提交**（已 gitignore）。

## Streamlit 自定义组件的坑（1.66）

- 组件的 default export 每次 `data` 变化都会重跑：事件处理用 `el.onxxx = ...` 赋值，不要 `addEventListener`（会越加越多）。
- 模块级的 `const` / `let` 放在文件顶层。写在函数体靠后位置的 `const`，首次渲染时就用到会落进暂时性死区，直接抛 ReferenceError（踩过多次）。
- `setStateValue` 要在 Python 侧有对应的 `on_xxx_change` 回调；组件不再渲染后它的状态会被清掉，要保留就在回调里另存到普通的 session_state 键。
- `.arb button` 的全局重置会盖掉单类选择器，自定义按钮样式要加 `.arb` 前缀。
- 对话流要逐条比对再更新，整体重建会闪。
- 新增回合阶段要在 `ui/payload.STATUS_TEXT` 配状态文案（有测试挡）。

## app.py 的坑

- `advance_turn()` 里不要给 `doc_id`、`meta` 赋值：它们是模块级变量，函数里一赋值，整个函数里都变成局部变量，前面的读取会抛 `UnboundLocalError`。
- 本地用 `python -m streamlit run app.py --server.port 8502`（PATH 上的 `streamlit` 可能是别的 Python 环境）。
  报「Port not available」却能打开页面，说明连上的是旧进程：用 `netstat -ano` 查 PID，旧的就换个端口。

## 抽取和模型

- pypdfium2 不能多线程解析同一份文档。
- `deepseek-flash` 是推理型模型，`max_tokens` 给小了正文会是空的。
- 金额核对按完整的数比，不能子串匹配；表格里常有两个数粘连（`23,902,060.2254,462,291.37`）、负数写成括号（万科）、
  只有一列有数的行。各模块开头的文档字符串列着具体遇到过的写法，改抽取规则前先看。

## 测试和评测

```bash
python -m pytest -q                         # 单元测试，不需要模型
python -m tools.eval_statements             # 三表抽取，不需要模型
python -m tools.eval_healthcheck            # 体检抽取，不需要模型
python -m tools.eval_intent --engine planner          # 规划器，60 题
python -m tools.eval_e2e --engines plain --workers 3 --tag x   # 端到端，37 题；并发 5 会连接报错
python -m tools.eval_review                 # 复核器
python -m tools.eval_drivers --tag x        # 几件大事
```

**上线评测**：改了 `rag/` 或 `ui/` 的上线，合并前跑 `python -m tools.release_eval --note "这次改了什么"`（约十几分钟）。
它按固定参数跑上面几项，结果追加一行到 `docs/eval/history.jsonl`，再调用 `tools/build_eval_snapshot.py` 重新生成评测页的数据
`docs/eval/snapshot.json`（两个文件都入库）。只改文档或 `web/` 时跳过。`--skip-model` 只跑不调模型的几项，用来快速自查。
评测页（`?eval=1`）只展示快照，线上从不跑评测；对照实验和里程碑写在 `tools/build_eval_snapshot.py` 的白名单里。

需要模型的评测用 `.env` 里的 Key，只用本地已有的报告（`data/`、`demo/`、`data/_samples/`，样本 PDF 不入库）。
结果写到 `eval_out/`（不入库），用真实模型跑会有波动，比较改动时多跑几轮、带上对照组（如 `--engines plain,nostmt`）。
