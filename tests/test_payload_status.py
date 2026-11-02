"""每个回合阶段都要有状态文案（ui/payload.STATUS_TEXT）。

前端把「除了完成和出错」的阶段都画成进行中（转圈 + 状态 + 读秒），文案从 STATUS_TEXT 来。
后端新加阶段忘了配文案时，界面会显示兜底的「检索相关片段…」—— 用这个测试挡住。
"""
import re
from pathlib import Path

from ui.payload import STATUS_TEXT, ai_message

ROOT = Path(__file__).resolve().parent.parent


def _statuses_set_in_app() -> set[str]:
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    return set(re.findall(r'target\["status"\]\s*=\s*"([a-z_]+)"', src)) - {"done", "error"}


def test_every_turn_status_has_a_status_text():
    found = _statuses_set_in_app()
    assert {"prestep", "latest", "retrieving", "history"} <= found     # 扫描本身没失效
    assert found <= set(STATUS_TEXT), found - set(STATUS_TEXT)


def test_chat_js_treats_every_unfinished_status_as_in_progress():
    js = (ROOT / "web" / "chat.js").read_text(encoding="utf-8")
    assert 'm.status !== "done" && m.status !== "error"' in js


def test_prestep_message_carries_its_step_text():
    msg = ai_message({"q": "x", "status": "prestep", "step_text": "第 1 步：先读 2026 半年报，记下要对比的数据…"})
    assert msg["status"] == "prestep" and msg["statusText"].startswith("第 1 步")
