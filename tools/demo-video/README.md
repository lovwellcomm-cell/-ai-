# 演示视频制作

视频在 `demo/` 下：

| 文件 | 说明 |
|---|---|
| `demo-90s.mp4` | 成片，1440×900 / 30fps / H.264，66 秒，约 1.6 MB |

> `post.py` 默认还会生成 `demo-teaser.gif` 和 `poster.png`；当前仓库只保留了成片，
> 需要时重跑 `python tools/demo-video/post.py` 即可再生成。
> 原始录屏 `demo-raw.mkv` 不随仓库提供，可用 `capture.ps1` 重新录制。

## 重新录制

```powershell
# 1) 生成片头 / 技术卡 / 片尾
python tools/demo-video/cards.py

# 2) 启动应用 + 录屏 + 执行演示脚本（约 2 分钟，期间不要动鼠标键盘）
powershell -ExecutionPolicy Bypass -File tools/demo-video/capture.ps1

# 3) 后期：加速、拼接卡片、烧字幕、导出 MP4/GIF
python tools/demo-video/post.py
```

只改字幕或加速比时，跳过第 2 步，直接跑第 3 步。

## 目录

```
tools/demo-video/
  scenario.json   演示动作时间轴（选择器 / 等待 / 停顿）
  record.mjs      Playwright 驱动 headed Chrome
  capture.ps1     起 Streamlit + FFmpeg gdigrab + 跑 record.mjs
  post.py         加速、拼接、ASS 字幕、导出
  cards.py        片头 / 技术卡 / 片尾（Pillow）
  cards/          生成的三张 PNG
  timeline.json   每次录制的真实动作时刻（post.py 用来对齐字幕）
```

## 几个坑

- **不要用 Playwright 自带的 `recordVideo`**：缺 `ms-playwright/ffmpeg-1011`，且不录鼠标指针。
- **录屏用 MKV 不用 MP4**：MP4 被强制结束时写不出 moov atom，整个文件损坏；MKV 是流式容器。
- **Chrome 必须加 `--disable-gpu`**：gdigrab 是 GDI 抓屏，读不到 GPU 合成层，会录成黑屏。
- **字幕时间必须从 `timeline.json` 算**：LLM 每次响应时长差很多（实测 6s 到 61s），写死时间轴会脱节。
- **`--app` 模式下 Playwright 拿不到页面句柄**，只能用普通窗口 + 固定 offset 裁掉标签栏。
- **PowerShell 脚本要存成 UTF-8 with BOM**，否则 5.1 按 ANSI 读，中文全乱。
- **Streamlit 的密码框要按 Enter 才提交**，只 `fill` 会停在 "Press Enter to apply" 状态。

## 演示模式

录制时用 `PUBLIC_DEMO=1` 启动，才能同时拿到：

- 侧边栏"使用自己的 DeepSeek Key"
- "40MB per file · PDF" 与"单个文件不超过 40 MB、400 页"
- 每会话剩余免费提问次数

同时 `SESSION_LIMIT=20` 避免演示中途额度用完。`demo/` 下两份年报都带 `demo: true`，
公开模式的 `cleanup_uploads()` 不会删它们。
