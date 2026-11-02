"""把开屏页（web/landing.html / .css / .js）打包成一个本地就能打开的预览页。

用法：python -m tools.build_landing_preview　生成 design/landing-preview.html，双击用浏览器打开。

开屏页本来是 Streamlit 组件：JS 是 `export default function (component)`，挂在 shadow root 里。
这里把三份文件原样内联，用一个假的 component（parentElement = document、setTriggerValue 只打日志）
调用它，所以预览里看到的就是线上会跑的那份代码，不是另写的示意。深浅色用页面自己右上角的按钮切换。
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
{css}
</style>
</head>
<body>
{html}
<script type="module">
{js}
const fake = {{ parentElement: document, data: {{ theme: "dark" }},
  setStateValue: (k, v) => {{ console.log("setStateValue", k, v);
    document.body.style.background = v === "dark" ? "#070809" : "#F6F5F2"; }},
  setTriggerValue: (k, v) => console.log("setTriggerValue", k, v) }};
landing(fake);
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
