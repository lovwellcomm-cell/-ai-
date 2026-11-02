"""把开屏页（web/landing.html / .css / .js）打包成一个本地就能打开的预览页。

用法：python -m tools.build_landing_preview　生成 design/landing-preview.html，双击用浏览器打开。

开屏页本来是 Streamlit 组件：JS 是 `export default function (component)`，挂在 shadow root 里。
这里把三份文件原样内联，用一个假的 component（parentElement = document、setTriggerValue 只打日志）
调用它，所以预览里看到的就是线上会跑的那份代码，不是另写的示意。右上角可以切换深浅色。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUT = ROOT / "design" / "landing-preview.html"

SHELL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>开屏页预览</title>
<style>
html, body {{ margin: 0; background: #070809; }}
.pv-theme {{ position: fixed; right: 16px; bottom: 16px; z-index: 100; padding: 6px 14px; border-radius: 999px;
  font: 13px -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif; cursor: pointer;
  color: #EDEEF0; background: rgba(20,22,25,.85); border: 1px solid rgba(255,255,255,.2); }}
{css}
</style>
</head>
<body>
{html}
<button class="pv-theme" id="pvTheme" type="button">切换浅色</button>
<script type="module">
{js}
let theme = "dark";
const fake = (t) => ({{ parentElement: document, data: {{ theme: t }},
  setTriggerValue: (k, v) => console.log("setTriggerValue", k, v) }});
let cleanup = landing(fake(theme));
document.getElementById("pvTheme").onclick = (e) => {{
  theme = theme === "dark" ? "light" : "dark";
  document.body.style.background = theme === "dark" ? "#070809" : "#F6F5F2";
  e.target.textContent = theme === "dark" ? "切换浅色" : "切换深色";
  cleanup = landing(fake(theme));
}};
</script>
</body>
</html>
"""


def main() -> None:
    js = (WEB / "landing.js").read_text(encoding="utf-8")
    assert "export default function" in js, "landing.js 的入口变了，改这个脚本"
    js = js.replace("export default function", "function landing", 1)
    OUT.write_text(SHELL.format(css=(WEB / "landing.css").read_text(encoding="utf-8"),
                                html=(WEB / "landing.html").read_text(encoding="utf-8"),
                                js=js), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
