"""演示视频后期：加速等待段 → 拼接片头/技术卡/片尾 → 烧中文字幕 → 导出 MP4 + GIF。

用法：
    python tools/demo-video/post.py
    python tools/demo-video/post.py --raw demo/demo-raw.mp4 --timeline tools/demo-video/timeline.json

产物：
    demo/demo-90s.mp4        1440×900 H.264，README 与面试播放
    demo/demo-teaser.gif     720 宽，约 12 秒，README 内嵌
    demo/poster.png          封面

设计要点：
  · 只对 LLM 等待段做加速，其余保持原速；字幕明确写"等待已加速"。
  · 字幕用 ASS（libass），不用 drawtext：多行、底框、定位都更可控。
  · 片头/技术卡/片尾是 Pillow 生成的 PNG，避免 drawtext 拼中文。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FFMPEG = (
    r"C:\Users\lovwe\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe"
)
FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe"))

W, H = 1440, 900
FPS = 30
TITLE_SEC = 4.0   # 片头时长

# ---------- 分段变速 ----------
# 只对"看等待"的段落加速，对"看操作"的段落保持原速或接近原速。
# 每段：(原始起, 原始止, 倍速)。起止由 timeline 里的真实时刻决定（见 segments_from_timeline）。
DEFAULT_SPEED = 1.55
# 首轮检索 + 生成：这次实测 61 秒，是整条片子里最没信息量的部分
FIRST_RETRIEVAL_SPEED = 3.2
# 自定义 Key 输入：逐字输入 + Enter 提交，放慢到原速，让观众看清每一步
KEY_INPUT_SPEED = 1.0


def segments_from_timeline(tl: dict) -> list[tuple[float, float, float]]:
    """按 timeline 的真实时刻切出变速段。返回 [(start, end, speed)]。"""
    marks: dict[int, dict[str, float]] = {}
    for rec in tl["timeline"]:
        step = rec.get("step")
        if step is None:
            continue
        marks.setdefault(int(step), {})[rec["phase"]] = rec["at"]

    q1 = marks.get(8, {})          # 第一次提问：waitFor 回答
    key = marks.get(50, {})        # 输入自定义 Key
    total = tl.get("duration", max(r["at"] for r in tl["timeline"]))

    cuts = [(0.0, total, DEFAULT_SPEED)]
    if "start" in q1 and "done" in q1:
        cuts = [
            (0.0, q1["start"], DEFAULT_SPEED),
            (q1["start"], q1["done"], FIRST_RETRIEVAL_SPEED),
            (q1["done"], total, DEFAULT_SPEED),
        ]
    if "start" in key and "done" in key:
        rebuilt: list[tuple[float, float, float]] = []
        for s, e, sp in cuts:
            if e <= key["start"] or s >= key["done"]:
                rebuilt.append((s, e, sp))
                continue
            if s < key["start"]:
                rebuilt.append((s, key["start"], sp))
            rebuilt.append((key["start"], key["done"], KEY_INPUT_SPEED))
            if e > key["done"]:
                rebuilt.append((key["done"], e, sp))
        cuts = rebuilt
    return [(s, e, sp) for s, e, sp in cuts if e - s > 0.01]


def make_mapper(cuts: list[tuple[float, float, float]]):
    """返回 raw 秒 → 成片秒 的分段线性映射。"""
    offsets = []
    acc = 0.0
    for s, e, sp in cuts:
        offsets.append((s, e, sp, acc))
        acc += (e - s) / sp
    total = acc

    def raw_to_out(t: float) -> float:
        for s, e, sp, off in offsets:
            if t <= e:
                return off + (max(t, s) - s) / sp
        return total

    return raw_to_out, total

# ---------- 字幕 ----------
# 每条字幕绑定到 scenario 里的一个步骤编号（step），起止时间由 timeline.json 的真实时刻算出。
# 这样 LLM 这次慢了 60 秒，字幕也会跟着走，不会出现"画面还在检索、字幕说已经答完"。
CAPTIONS_BY_STEP = [
    (0, 4.0, "两份 200+ 页年报已建索引"),
    (3, 2.4, "选择要分析的文档"),
    (6, 2.4, "自然语言提问，不用记指标在哪一页"),
    (8, None, "检索 + 生成（等待已加速）"),          # 止于该步完成
    (11, 2.6, "每个数字都标了出处"),
    (14, 3.0, "点开引用：章节、页码、原文片段"),
    (17, 3.2, "一键翻到 PDF 原页核对"),
    (23, 2.4, "同一问题可继续追问"),
    (26, 1.0, "自动识别「去年」= 2024 年"),
    (31, 2.6, "切换文档，对话按文档隔离"),
    (37, 3.2, "首轮回答后追加「为什么」"),
    (48, 3.0, "可换成自己的 API Key"),
    (50, 2.8, "单文件 40 MB / 400 页"),
    (58, 3.0, "结论 + 数据依据 + 归因分析"),
]

# 某些动作点击后页面要 rerun 1-2 秒才变样，字幕应等画面变了再出现（秒，原始时间）
# 26(追问) 和 31(切换文档) 之间只有 1.2s，原样会让"追问"字幕撞上"切换文档"画面。
# 50(Key 输入) 原速 5s，字幕必须覆盖整段输入过程。
CAPTION_DELAY = {26: -1.2, 31: 1.6, 37: 0.8, 50: 0.0}
CAPTION_MIN = {50: 5.0}


def captions_from_timeline(tl: dict, raw_to_out) -> list[tuple[float, float, str]]:
    """把 (step, 时长, 文案) 换算成成片时间（已含分段变速）。

    起止取该 step 在原始录像里的真实时刻，再经 raw_to_out 映射；
    时长按该处的实际倍速缩放。hold=None 表示持续到该 step 结束。
    """
    marks: dict[int, dict[str, float]] = {}
    for rec in tl["timeline"]:
        step = rec.get("step")
        if step is None:
            continue
        marks.setdefault(int(step), {})[rec["phase"]] = rec["at"]

    out = []
    for step, hold, text in CAPTIONS_BY_STEP:
        m = marks.get(step)
        if not m or "start" not in m:
            continue
        raw_start = max(m["start"] + CAPTION_DELAY.get(step, 0.0), 0.0)
        start = raw_to_out(raw_start)
        if hold is None:
            end = raw_to_out(m.get("done", m["start"] + 3.0))
        else:
            # hold 以"默认倍速下的秒数"表示，换回原始秒再映射
            end = raw_to_out(raw_start + max(hold, CAPTION_MIN.get(step, 0.0)) * DEFAULT_SPEED)
        out.append((start, max(end, start + 1.2), text))

    # 防止重叠：下一条的起点必须晚于上一条的终点（留 0.05s 间隙）
    for i in range(1, len(out)):
        prev_end = out[i - 1][1]
        s, e, t = out[i]
        if s < prev_end + 0.05:
            shift = prev_end + 0.05 - s
            s += shift
            e += shift
            out[i] = (s, e, t)
    return out


def clamp_captions(caps: list[tuple[float, float, str]], body_dur: float):
    """把字幕压进正片时间范围内，避免盖到技术卡/片尾卡上。"""
    out = []
    for s, e, t in caps:
        if s >= body_dur:
            continue
        out.append((s, min(e, body_dur), t))
    return out


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Microsoft YaHei,28,&H00FFFFFF,&H00FFFFFF,&H99000000,&H99000000,0,0,0,0,100,100,0,0,3,10,0,8,80,80,16,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(path: Path, captions: list[tuple[float, float, str]], offset: float) -> None:
    lines = [ASS_HEADER.format(w=W, h=H)]
    for start, end, text in captions:
        lines.append(
            f"Dialogue: 0,{ts(start + offset)},{ts(end + offset)},Cap,,0,0,0,,"
            f"{{\\fad(180,180)}}{text}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- ffmpeg ----------
def run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-3000:])
        raise SystemExit(f"ffmpeg 失败（{proc.returncode}）")


def start_trim(raw: Path) -> float:
    """算出开头要裁掉多少秒。

    录屏在 Chrome 还没渲染出应用时就已经开始，那段画面是 VS Code / 白屏，
    必须裁掉。差值来自两个墙钟文件：
      ffmpeg-start.json   ffmpeg 进程启动时刻
      timeline.ready      Playwright 确认应用真正可见的时刻
    """
    start_file = HERE / "ffmpeg-start.json"
    ready_file = HERE / "timeline.ready"
    if not start_file.exists() or not ready_file.exists():
        return 0.0
    try:
        started = json.loads(start_file.read_text(encoding="utf-8"))["startedAt"]
        ready = json.loads(ready_file.read_text(encoding="utf-8"))["readyAt"]
    except Exception:
        return 0.0
    trim = (ready - started) / 1000.0
    # ffmpeg 进程启动到第一帧还有约 0.5s 延迟，这里直接按差值裁并留 0.3s 余量
    return max(0.0, trim - 0.3)


def duration_of(path: Path) -> float:
    """读时长。MKV 被强杀后没有 duration 元数据，退回按帧数计算。"""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        pass
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return int(probe) / FPS


def card_clip(png: Path, seconds: float, out: Path) -> None:
    """静态卡片 → 视频片段。"""
    run([
        FFMPEG, "-y", "-loop", "1", "-t", f"{seconds}", "-i", str(png),
        "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        str(out),
    ])


def concat(parts: list[Path], out: Path) -> None:
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(out)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(ROOT / "demo" / "demo-raw.mkv"))
    ap.add_argument("--timeline", default=str(HERE / "timeline.json"))
    ap.add_argument("--out", default=str(ROOT / "demo" / "demo-90s.mp4"))
    args = ap.parse_args()

    raw = Path(args.raw)
    if not raw.exists():
        raise SystemExit(f"找不到原始录制：{raw}\n先运行 tools/demo-video/capture.ps1")

    cards = HERE / "cards"
    for name in ("title.png", "tech.png", "end.png"):
        if not (cards / name).exists():
            raise SystemExit(f"缺少卡片 {cards / name}\n先运行 python tools/demo-video/cards.py")

    tl = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
    trim = start_trim(raw)
    raw_dur = duration_of(raw)
    print(f"原始录制：{raw_dur:.1f}s，开头裁掉 {trim:.1f}s（应用渲染前的 VS Code / 白屏）")

    tmp = Path(tempfile.mkdtemp(prefix="demo-post-"))

    # 1) 正片：分段变速（等待段快、Key 输入段原速），逐段渲染后拼接
    #    先把 timeline 里的时刻整体减去 trim，再切段，避免开头空窗进入成片
    tl_shifted = {
        "duration": max(tl.get("duration", raw_dur) - trim, 0.1),
        "timeline": [
            {**r, "at": r["at"] - trim}
            for r in tl["timeline"]
            if r["at"] - trim >= 0
        ],
    }
    cuts = segments_from_timeline(tl_shifted)
    raw_to_out, body_dur = make_mapper(cuts)
    print("分段变速：" + " | ".join(f"{s:.1f}-{e:.1f}s@{sp}x" for s, e, sp in cuts))

    body = tmp / "body.mp4"
    pieces = []
    for i, (s, e, sp) in enumerate(cuts):
        piece = tmp / f"part{i:02d}.mp4"
        run([
            FFMPEG, "-y",
            "-ss", f"{s + trim:.3f}", "-to", f"{e + trim:.3f}", "-i", str(raw),
            "-vf", f"crop={W}:{H - 28}:0:0,setpts=PTS/{sp},fps={FPS},"
                   f"scale={W}:{H},format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            str(piece),
        ])
        pieces.append(piece)
    concat(pieces, body)
    print(f"正片：{duration_of(body):.1f}s（原始 {raw_dur:.1f}s − 片头 {trim:.1f}s）")

    # 2) 三张卡片
    title = tmp / "title.mp4"
    tech = tmp / "tech.mp4"
    end = tmp / "end.mp4"
    card_clip(cards / "title.png", TITLE_SEC, title)
    card_clip(cards / "tech.png", 8.0, tech)
    card_clip(cards / "end.png", 7.0, end)

    # 3) 拼接
    joined = tmp / "joined.mp4"
    concat([title, body, tech, end], joined)

    # 4) 烧字幕（时间轴由真实 mark 经分段变速映射换算）
    ass = tmp / "subs.ass"
    caps = captions_from_timeline(tl_shifted, raw_to_out)
    # 字幕先按正片时长裁，再加片头偏移；否则会把字幕留在技术卡上
    caps = clamp_captions(caps, body_dur)
    write_ass(ass, caps, offset=TITLE_SEC)
    print(f"字幕 {len(caps)} 条，末条结束于 {caps[-1][1] + TITLE_SEC:.1f}s / 正片止于 {body_dur + TITLE_SEC:.1f}s")
    final = Path(args.out)
    final.parent.mkdir(parents=True, exist_ok=True)

    # Windows 路径在 filter 里要转义：反斜杠 + 盘符冒号
    ass_arg = str(ass).replace("\\", "/").replace(":", "\\:")
    fonts = "C\\:/Windows/Fonts"
    run([
        FFMPEG, "-y", "-i", str(joined),
        "-vf", f"ass=filename='{ass_arg}':fontsdir='{fonts}'",
        "-c:v", "libx264", "-preset", "slow", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
        str(final),
    ])
    print(f"成片：{final}  {final.stat().st_size / 1e6:.2f} MB  {duration_of(final):.1f}s")

    # 5) 封面
    poster = ROOT / "demo" / "poster.png"
    run([FFMPEG, "-y", "-i", str(cards / "title.png"), "-frames:v", "1", str(poster)])

    # 6) 短 GIF（前 12 秒：片头 + 第一次提问与引用）
    gif = ROOT / "demo" / "demo-teaser.gif"
    run([
        FFMPEG, "-y", "-t", "12", "-i", str(final),
        "-vf", "fps=12,scale=720:-1:flags=lanczos,split[a][b];"
               "[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
        "-loop", "0", str(gif),
    ])
    print(f"预告 GIF：{gif}  {gif.stat().st_size / 1e6:.2f} MB")

    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
