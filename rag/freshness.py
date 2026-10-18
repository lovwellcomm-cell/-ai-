"""部署期模块新鲜度守卫：让进程里跑的模块和磁盘上的源码对齐。

Streamlit Community Cloud 做原地更新时不一定重启进程。`app.py` 每个 script run 都被
重新执行（Streamlit 每次都新建一个 `__main__`），`web/*` 也是运行时按 mtime 读盘，
所以这两处永远是新的；但 `rag/*.py`、`ui/*.py` 是**导入过**的模块，躺在 `sys.modules`
里，除非被显式卸载，否则整个进程生命周期都用旧代码。

这里每次运行比对一次源码内容戳，对不上就把整个 `rag`/`ui` 从 `sys.modules` 摘掉，
让 `app.py` 下面那些 import 重新从磁盘执行：

    from rag.freshness import ensure_fresh
    SOURCE_STAMP = ensure_fresh()      # 必须早于其他项目模块的导入
    from rag import config             # 这一行开始才是新代码

为什么是「摘掉」而不是 `importlib.reload`：`from x import y` 在导入方留下的是对象绑定，
`reload(x)` 不会更新这些绑定（本仓库里 `ui/payload.py`、`ui/turn.py`、`rag/external.py`
都是这种写法），逐个 reload 还得自己排依赖顺序，中途失败会留下新旧混杂的模块。
整体卸载后由导入机制重新执行，依赖顺序和绑定关系自然正确。

**稳定契约**：`ensure_fresh` 不 import 任何项目内模块、只依赖标准库、返回 str。
这个文件自己也在被卸载的包里，所以升级后的第一次运行用的仍是上一版实现 ——
旧实现必须还能工作。`app.py` 顶层只导入 `ensure_fresh` 这一个符号，
其余（`build_report`、`take_cache_bust`）按需延迟导入。
"""
from __future__ import annotations

import builtins
import hashlib
import importlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
TRACKED_DIRS = ("rag", "ui")
EVICT_PACKAGES = ("rag", "ui")

ENV_STAMP = "ARB_SOURCE_STAMP"
ENV_PREV_STAMP = "ARB_PREV_STAMP"
ENV_RELOAD_AT = "ARB_RELOAD_AT"
ENV_RELOAD_COUNT = "ARB_RELOAD_COUNT"
ENV_PROCESS_STARTED = "ARB_PROCESS_STARTED"
ENV_CACHE_BUST = "ARB_CACHE_BUST"

_LOCK_NAME = "_arb_source_reload_lock"


def tracked_sources() -> list[Path]:
    """参与指纹的项目源文件：rag/ 与 ui/ 下的 .py。"""
    out: list[Path] = []
    for name in TRACKED_DIRS:
        out.extend(sorted((ROOT / name).glob("*.py")))
    return out


def source_stamp(paths: Iterable[Path] | None = None) -> str:
    """按「相对路径 + 文件内容」算一个短摘要；任何文件读不到就返回空串（不判断）。

    不用 mtime / size：部署工具（tar、rsync、镜像层）可能保留时间戳，
    内容哈希才是真相。全部文件加起来一百多 KB，每个 rerun 读一遍可以忽略。
    """
    files = sorted((Path(p) for p in (paths if paths is not None else tracked_sources())),
                   key=lambda p: str(p).lower())
    h = hashlib.sha256()
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        try:
            rel = path.resolve().relative_to(ROOT).as_posix()
        except ValueError:                     # 仓库外的文件（测试里的临时目录）
            rel = path.resolve().as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(data)).encode("ascii"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()[:12]


def project_modules() -> list[str]:
    """`sys.modules` 里属于 rag / ui 的键（含包本身与子模块）。"""
    return [n for n in list(sys.modules) if n.split(".", 1)[0] in EVICT_PACKAGES]


def _lock() -> threading.Lock:
    """锁必须活在 builtins 里：本模块自己也会被卸载重导，模块级变量随之归零。"""
    lock = getattr(builtins, _LOCK_NAME, None)
    if lock is None:
        lock = threading.Lock()
        setattr(builtins, _LOCK_NAME, lock)
    return lock


def _evict() -> list[str]:
    """把 rag / ui 连同子模块整体从 sys.modules 摘掉，返回摘掉的键。

    必须连子模块一起摘（前缀匹配而不是 `startswith("rag")`），否则会留下
    指向旧父包的孤儿模块。
    """
    doomed = project_modules()
    for name in doomed:
        sys.modules.pop(name, None)
    return doomed


def ensure_fresh(paths: Iterable[Path] | None = None) -> str:
    """在导入其他项目模块之前调用，返回本次运行对应的源码戳。

    · 进程第一次运行（环境里还没有戳）：只记录，不卸载 —— 此时模块本来就是新导入的；
    · 戳没变：直接返回，每次 rerun 只多读一遍源码；
    · 戳变了：卸载 rag / ui，并留下「本次要清缓存」的标记给 app.py。
    """
    stamp = source_stamp(paths)
    if not stamp:                                  # 源文件读不到就保持现状
        return os.environ.get(ENV_STAMP, "")

    with _lock():
        os.environ.setdefault(ENV_PROCESS_STARTED, str(int(time.time())))
        prev = os.environ.get(ENV_STAMP, "")
        if not prev:
            os.environ[ENV_STAMP] = stamp
            return stamp
        if prev == stamp:
            return stamp

        os.environ[ENV_PREV_STAMP] = prev
        os.environ[ENV_RELOAD_AT] = str(int(time.time()))
        os.environ[ENV_RELOAD_COUNT] = str(int(os.environ.get(ENV_RELOAD_COUNT, "0") or 0) + 1)
        os.environ[ENV_CACHE_BUST] = stamp
        _evict()
        importlib.invalidate_caches()
        os.environ[ENV_STAMP] = stamp
    return stamp


def take_cache_bust(stamp: str) -> bool:
    """取走「需要清缓存」的标记：只有刚卸载过模块的那一次运行返回 True。"""
    if os.environ.get(ENV_CACHE_BUST) == stamp:
        os.environ.pop(ENV_CACHE_BUST, None)
        return True
    return False


def build_report(stamp: str = "") -> dict:
    """`?build=1` 面板用的诊断数据，只读、不落盘。"""
    src = stamp or source_stamp()
    imported = os.environ.get(ENV_STAMP, "")
    count = 0
    newest = 0.0
    for path in tracked_sources():
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        newest = max(newest, stat.st_mtime)

    started = _to_int(os.environ.get(ENV_PROCESS_STARTED, ""))
    reloads = _to_int(os.environ.get(ENV_RELOAD_COUNT, ""))
    survived = bool(started and newest and started < newest)

    if not survived:
        verdict = "进程在文件更新之后启动：之前看到的旧行为来自部署完成前的旧进程，刷新页面即可。"
    elif src == imported and reloads:
        verdict = "进程跨部署存活，本次已按源码戳卸载旧模块并重新导入。"
    else:
        verdict = "进程跨部署存活，但模块没有按预期重载 —— 检查 ensure_fresh 是否被调用。"

    return {
        "source_stamp": src,
        "imported_stamp": imported,
        "prev_stamp": os.environ.get(ENV_PREV_STAMP, ""),
        "stamps_match": src == imported,
        "reload_count": reloads,
        "reloaded_at": _iso(os.environ.get(ENV_RELOAD_AT, "")),
        "process_started": _iso(os.environ.get(ENV_PROCESS_STARTED, "")),
        "newest_source_mtime": _iso(newest),
        "process_survived_deploy": survived,
        "verdict": verdict,
        "modules": sorted(project_modules()),
        "tracked_files": count,
    }


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso(value: str | float) -> str:
    """时间戳转本地时间字符串；空值或非法值返回空串。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
