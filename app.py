"""年报 AI 研究助手 —— Streamlit 界面（运行：streamlit run app.py）

界面分两块：
  · 左侧文档栏 = 原生 Streamlit 控件，只有它负责上传（40MB 的 PDF 走组件桥会在每次
    rerun 被重新序列化，不划算）。样式由 web/streamlit-shell.css 刷成设计稿的样子。
  · 主区对话面板 = ui/chat_component.py 里的 st.components.v2 自定义组件。

一次提问按「回合状态机」跑四到五轮脚本（见 advance_turn）。这么做是为了在整段呈现回答的前提下，
仍然给出真实的分阶段状态文案，而不是拿假计时器糊弄。
"""
import base64
import json
import os
import time

import streamlit as st

# 部署不一定重启进程：app.py 每次 rerun 都从磁盘重读，rag/*.py 与 ui/*.py 却留在
# sys.modules 里。下面这行必须在**其他项目模块之前**执行，比对源码戳并在需要时
# 卸载旧模块（见 rag/freshness.py）。
from rag.freshness import ensure_fresh

SOURCE_STAMP = ensure_fresh()

from rag import config
from rag.chunker import chunk_pages
try:
    from rag.disclosure import ReportNotFound, download_pdf, ensure_report, source_site
except Exception as _import_error:          # 部署环境里的 import 失败要能被看见
    st.error(f"模块导入失败：{type(_import_error).__name__}: {_import_error}"
             f"（源码戳 {SOURCE_STAMP}，重载于 {os.environ.get('ARB_RELOAD_AT', '未重载')}）")
    raise
from rag.external import answer_with_tools
from rag.financials import load_or_build as load_financials
from rag.healthcheck import load_or_build as load_health, merge_quarter, wants_health
from rag import drivers as key_drivers
from rag import statements as three_statements
from rag.generator import analyze, generate, get_client, make_client, resolve_style
from rag import caliber, planner, reviewer
from rag.history import (as_hit as history_hit, attach as attach_history, is_hist_id, load_older,
                         load_report, merge, older_year, page_png as hist_page_png, wants_trend)
from rag.quarters import (as_hit as quarter_hit, attach as attach_quarters, build as build_quarters,
                          wants_quarters)
from rag import notes as fact_notes
from rag.fidelity import unverified_in_tables
from rag.industry import RETRIEVAL_QUERY as INDUSTRY_QUERY, industry_brief, wants_industry
from rag.indexer import (build_index, cleanup_uploads, doc_dir, doc_id_of, ensure_demo,
                         is_current, list_documents, load_index)
from rag.limits import Quota, check_and_consume
from rag.pdf_parser import extract_doc_info, parse_pdf, render_page_png
from rag.periods import FY, SHORT, not_out_yet, period_of, report_label
from rag.retriever import Retriever
from ui import payload
from ui import turn as turn_core
from ui import memory as chat_memory
from ui.card import build_card, wants_card
from ui.chat_component import chat_surface

st.set_page_config(page_title="年报 AI 研究助手", page_icon="📊", layout="wide")

# ---------- 临时构建自检（?build=1） ----------
# 用来分辨两种「线上还是旧代码」：
#   · process_started 晚于文件更新时间 → 你连的是部署之前的旧进程，刷新即可；
#   · 进程跨部署存活 → 看 reload_count，说明 freshness 守卫刚刚卸载并重导了模块。
# 放在这里（而不是文件末尾）是为了在 st.stop() 或异常之前就能看到。只读、不落盘。
if str(st.query_params.get("build", "")) == "1":
    from rag.freshness import build_report

    with st.sidebar:
        with st.expander("构建自检（临时）", expanded=True):
            st.caption("当前进程跑的是哪一份代码。stamp 与本地一致即说明线上已是最新。")
            st.json(build_report(SOURCE_STAMP))

SHELL_CSS = """
  /* 1. 隐藏 Streamlit 自带顶栏。侧边栏折叠后，展开按钮仍在这个 header 里，
        程序化 click 它照样能打开侧边栏（已实测），所以组件自带的菜单按钮就够用。 */
  [data-testid="stHeader"] { display: none !important; }

  /* 2. 清零主区留白。官方推荐用 [theme] paddingTop/paddingBottom，但在
        streamlit==1.66.0 实测前端没吃这个值（仍是默认 6rem/10rem），
        且 paddingLeft/Right 官方明确不认，所以这里统一用 CSS。 */
  [data-testid="stMainBlockContainer"] {
    padding: 0 !important;
    max-width: none !important;
  }

  /* 3. 注入本样式表用的空 markdown 块不参与布局，否则 stVerticalBlock 的 16px gap
        会把组件往下推，页面就多出 16px 可滚动高度。 */
  [data-testid="stElementContainer"]:has(> [data-testid="stMarkdown"] style) {
    display: none !important;
  }

  /* 4. 侧边栏向设计稿的左轨靠拢。选择器集中在这里，万一将来 Streamlit 改类名，
        后果是回落成默认样式，而不是界面崩掉。 */
  [data-testid="stSidebar"] {
    background: var(--arb-rail);
    border-right: 1px solid var(--arb-line);
  }
  [data-testid="stSidebar"] [data-testid="stSidebarHeader"] { padding-bottom: 0; }
  [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3,
  [data-testid="stSidebar"] p, [data-testid="stSidebar"] label, [data-testid="stSidebar"] span,
  [data-testid="stSidebar"] .stMarkdown { color: var(--arb-ink); }
  [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
  [data-testid="stSidebar"] small { color: var(--arb-ink-3) !important; font-size: 11px; }
  [data-testid="stSidebar"] .stButton > button {
    justify-content: flex-start;
    text-align: left;
    border-radius: 11px;
    border: 1px solid transparent;
    background: transparent;
    color: var(--arb-ink-2);
    padding: 9px 11px;
    font-size: 13px;
    font-weight: 500;
    line-height: 1.35;
  }
  [data-testid="stSidebar"] .stButton > button:hover {
    background: var(--arb-surface-2);
    border-color: transparent;
    color: var(--arb-ink);
  }
  [data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: var(--arb-surface-2);
    border-color: var(--arb-accent-line);
    color: var(--arb-ink);
  }
  [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
    background: var(--arb-surface);
    border: 1px dashed var(--arb-line-2);
    border-radius: 11px;
  }
  [data-testid="stSidebar"] hr { margin: 10px 0; border-color: var(--arb-line); }

  /* 5. rerun 期间 Streamlit 会把「本轮还没重新渲染」的元素标成 stale，按
        opacity:.33 + 1s 淡入过渡整块发灰（streamlit 前端写死的样式）。
        对话面板在一次 rerun 里排在检索器加载之后才渲染，问答、翻原页、切年报
        都会先灰上 1~3 秒，看起来像页面卡死或蒙了一层雾。面板自己有分阶段的
        状态文案，「正在…」比整块变淡更准确，所以只对这一个组件关掉。
        类名来自挂载时的 key="chat"（stElementContainer 会带 st-key-chat）。 */
  [data-testid="stElementContainer"].st-key-chat[data-stale="true"] {
    opacity: 1 !important;
    transition: none !important;
  }
"""

# 侧边栏用的令牌。注意必须整段放在 <style> 标签**里面**，
# 否则会被 markdown 当成正文文本，变量不生效、回落到兜底色。
RAIL_TOKENS = {
    "light": ("--arb-rail:#F2F2F4;--arb-surface:#FFFFFF;--arb-surface-2:#F5F5F7;"
              "--arb-line:rgba(12,13,15,.09);--arb-line-2:rgba(12,13,15,.16);"
              "--arb-ink:#0C0D0F;--arb-ink-2:#5A5F66;--arb-ink-3:#8A8F96;"
              "--arb-accent-line:rgba(138,102,8,.34);"),
    "dark": ("--arb-rail:#0F1114;--arb-surface:#14171A;--arb-surface-2:#1A1E22;"
             "--arb-line:rgba(255,255,255,.075);--arb-line-2:rgba(255,255,255,.15);"
             "--arb-ink:#E9EBEE;--arb-ink-2:#9BA2AB;--arb-ink-3:#6B727B;"
             "--arb-accent-line:rgba(224,178,78,.32);"),
}


def shell_style(theme: str) -> str:
    tokens = RAIL_TOKENS["dark" if theme == "dark" else "light"]
    return f"<style>:root{{{tokens}}}{SHELL_CSS}</style>"


@st.cache_resource
def get_quota() -> Quota:
    """全站共用的每日额度计数器（进程内共享）。

    刻意不带源码戳：模块重载时若换掉这个对象，当日已用次数会被清零。
    旧模块里的 Quota 实例照样能用 —— check_and_consume 只按鸭子类型调用它。
    """
    return Quota(config.DAILY_LIMIT)


@st.cache_resource(ttl=3600)
def startup(stamp: str) -> None:
    """进程启动（或部署后的第一次运行）时复制内置示例年报、清理过期上传。"""
    ensure_demo()
    if config.PUBLIC_DEMO:
        cleanup_uploads(config.UPLOAD_TTL_HOURS)


startup(SOURCE_STAMP)


@st.cache_resource(show_spinner="加载索引…")
def get_retriever(doc_id: str, index_version: int, stamp: str):
    """stamp 进缓存键：部署后自动换掉用旧代码构造的 Retriever。

    参数名不能带前导下划线 —— Streamlit 会把 `_` 开头的参数排除在缓存键之外。
    """
    meta, chunks, emb = load_index(doc_id)
    return meta, Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))


@st.cache_data(show_spinner=False)
def render_page(doc_id: str, page_no: int, stamp: str) -> bytes:
    return render_page_png(doc_dir(doc_id) / "source.pdf", page_no)


@st.cache_data(show_spinner=False)
def page_data_uri(doc_id: str, page_no: int, stamp: str) -> str:
    """渲染原页并转成 data URI。base64 一起缓存，免得每次 rerun 重新编码。"""
    return "data:image/png;base64," + base64.b64encode(
        render_page(doc_id, page_no, stamp)).decode("ascii")


@st.cache_data(show_spinner=False)
def get_financials(doc_id: str, stamp: str) -> dict | None:
    """「主要会计数据」的结构化抽取结果（见 rag/financials.py）。
    第一次用到时从 source.pdf 抽取并写成 financials.json，老文档不需要重建索引。"""
    return load_financials(doc_id)


@st.cache_data(show_spinner=False)
def get_health(doc_id: str, stamp: str) -> dict | None:
    """年报小字体检（rag/healthcheck.py）。第一次做时读全文（约 1 秒），写成 healthcheck.json。"""
    return load_health(doc_id, get_financials(doc_id, stamp))


@st.cache_data(show_spinner=False)
def get_statements(doc_id: str, stamp: str) -> dict | None:
    """三张合并报表的抽取和勾稽核对（rag/statements.py）。第一次做时读全文（约 1 秒），写成 statements.json。"""
    return three_statements.load_or_build(doc_id)


# 刚卸载过旧模块的那一次运行，把旧代码构造的缓存对象清掉（只清这几个，
# 绝不调用 st.cache_resource.clear() —— 那会连 get_quota 一起清、额度归零）。
from rag.freshness import take_cache_bust  # noqa: E402  （重载后这里拿到的是新版本）

if take_cache_bust(SOURCE_STAMP):
    get_retriever.clear()
    render_page.clear()
    page_data_uri.clear()
    get_financials.clear()
    get_health.clear()
    get_statements.clear()


def _patch_meta(doc_id: str, company: str = "", year: str = "",
                source_url: str = "", source_site: str = "", name: str = "",
                period: str = "") -> None:
    """索引已存在时补齐元数据（自动导入知道公司/年份，但从 PDF 里可能抽不出来）。"""
    path = doc_dir(doc_id) / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for key, val in (("company", company), ("year", str(year or "")),
                     ("source_url", source_url), ("source_site", source_site),
                     ("name", name), ("period", "" if period == FY else period)):
        if val and meta.get(key) != val:
            meta[key] = val
            changed = True
    if changed:
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def ingest(data: bytes, name: str, demo: bool = False, check_limits: bool = False,
           source_url: str = "", source_site: str = "",
           company: str = "", year: str = "", period: str = ""):
    """解析并建立索引。返回 (doc_id, 错误信息)；失败时 doc_id 为 None。

    company / year / period 是调用方已知的覆盖值（自动导入时来自巨潮公告），
    优先于从 PDF 封面里认出来的。
    """
    did = doc_id_of(data)
    if is_current(did):
        _patch_meta(did, company, str(year or ""), source_url, source_site, name, period)
        return did, ""
    with st.status(f"处理 {name}", expanded=True) as status:
        st.write("② 提取文本、页码与章节…")
        pages = parse_pdf(data)
        if check_limits and len(pages) > config.MAX_PAGES:
            status.update(label="页数超限", state="error")
            return None, f"该 PDF 有 {len(pages)} 页，超过在线演示的上限 {config.MAX_PAGES} 页。"
        if sum(len(p.text) for p in pages) < 100:
            status.update(label="未提取到文本", state="error")
            return None, "该 PDF 几乎没有可提取文本，可能是扫描件，当前版本不支持 OCR。"
        st.write(f"共 {len(pages)} 页。③ 切分文本…")
        info = extract_doc_info(pages)
        eff_company = company or info.company
        eff_year = str(year or info.year or "")
        info.period = period or info.period
        chunks = chunk_pages(pages, config.CHUNK_SIZE, config.CHUNK_OVERLAP, info)
        st.write(f"{len(chunks)} 个片段。④ 向量化并建立索引（首次需下载模型）…")
        build_index(did, data, name, len(pages), chunks, company=eff_company, year=eff_year,
                    demo=demo, source_url=source_url, source_site=source_site,
                    period=info.period)
        status.update(label=f"{name} 已建立索引", state="complete", expanded=False)
    return did, ""


def doc_label(d: dict) -> str:
    """按钮显示名：“精达股份 2025”；季报 / 半年报加上期：“精达股份 2026 三季报”。"""
    who = f"{d.get('company', '')} {d.get('year', '')}".strip()
    period = period_of(d)
    if who and period != FY:
        who += f" {SHORT[period]}"
    return who or d["name"]


def import_report(company: str, year, period: str, newer=None) -> tuple[str, str]:
    """去巨潮下载一份报告并建索引，返回 (文档 id, 「精达股份 2026 三季报」)。

    找不到或建索引失败时抛异常，提示语由调用方决定（主报告失败要退款报错，前置步骤失败只记一笔）。
    「最新财报」：newer 是报告期已结束、可能提前披露的下一期，先试它，没有再用 (year, period)。
    """
    attempts = ([tuple(newer)] if newer else []) + [(year, period)]
    for i, (want_year, want_period) in enumerate(attempts):
        try:
            data, fname, info = ensure_report(company, want_year, want_period)
            period = want_period
            break
        except ReportNotFound:
            if i == len(attempts) - 1:
                raise
    auto_company = info.get("company") or company
    auto_year = str(info.get("year") or "")
    label = f"{auto_company} {report_label(auto_year, period)}".strip()
    new_id, err = ingest(data, f"{label}.pdf" if label else fname,
                         check_limits=config.PUBLIC_DEMO,
                         source_url=info["url"],
                         source_site=source_site(info["url"]),
                         company=auto_company, year=auto_year,
                         period=period)
    if not new_id:
        raise RuntimeError(err)
    visible = visible_ids()
    if visible is not None:
        visible.add(new_id)
    return new_id, label


def uploads_used() -> int:
    """本会话导入/上传了几份（计入上限）。刷新后从浏览器恢复回来的报告不算新导入。"""
    return len(st.session_state.get("my_docs", set()) - st.session_state.get("restored_docs", set()))


def visible_ids():
    """公开模式下，每个会话只能看到示例年报和自己上传的；本地模式看到全部。"""
    return st.session_state.setdefault("my_docs", set()) if config.PUBLIC_DEMO else None


def keep_theme(component_key: str) -> None:
    """开屏页 / 聊天页右上角切深浅色的回调：组件发来 {t, at}，记进两页共用的 ui_theme。"""
    v = st.session_state.get(component_key, {}).get("theme")
    t = v.get("t") if isinstance(v, dict) else v
    if t in ("dark", "light"):
        st.session_state["ui_theme"] = t


def current_theme() -> str:
    """组件的深浅色：优先用页面上手动切换的结果（开屏页、聊天页谁最后切听谁的），否则跟随 Streamlit 主题。
    只认 ui_theme 这一个键：组件自己的状态在它不渲染后不一定马上清掉，分开读会互相盖（实测过）。"""
    if st.session_state.get("ui_theme") in ("dark", "light"):
        return st.session_state["ui_theme"]
    try:
        return st.context.theme.type or "light"
    except Exception:
        return "light"


# ---------- 开屏页 ----------
# 第一次打开先看开屏页，点「开始聊天」后在网址上记 ?chat=1：刷新、分享链接都直接进助手。
# 放在侧边栏之前、st.stop() 收尾：开屏页不加载检索器和模型，打开得快。
# ?build=1 自检页、以及这次会话里已经进过助手的，都不再显示开屏页。
LANDING_CSS = """
  [data-testid="stHeader"], [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
  [data-testid="stExpandSidebarButton"] { display: none !important; }
  [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: none !important; }
  [data-testid="stElementContainer"]:has(> [data-testid="stMarkdown"] style) { display: none !important; }
  [data-testid="stAppViewContainer"], [data-testid="stMain"] { background: transparent; }
"""
# ---------- 评测页（?eval=1） ----------
# 只展示仓库里的评测快照（docs/eval/snapshot.json，tools/build_eval_snapshot.py 生成），不加载检索器和模型。
# 顶栏「首页」「开始聊天」由组件发 go，这里改网址再重跑
if str(st.query_params.get("eval", "")) == "1":
    from ui.eval_component import eval_surface, load_snapshot

    _theme = current_theme()
    _bg = "#070809" if _theme == "dark" else "#F6F5F2"
    st.markdown(f"<style>{LANDING_CSS} .stApp {{ background: {_bg}; }}</style>", unsafe_allow_html=True)
    _ev = eval_surface(dict(load_snapshot(), theme=_theme))
    _go = getattr(_ev, "go", None)
    if _go in ("home", "chat"):
        st.query_params.pop("eval", None)
        # 「首页」回开屏页：这次会话进过助手也要清掉标记，不然开屏页那段会直接跳过
        st.session_state["entered"] = _go == "chat"
        if _go == "chat":
            st.query_params["chat"] = "1"
        else:
            st.query_params.pop("chat", None)
        st.rerun()
    st.stop()

if (not st.session_state.get("entered")
        and str(st.query_params.get("chat", "")) != "1"
        and str(st.query_params.get("build", "")) != "1"):
    from ui.landing_component import landing_surface

    _theme = current_theme()
    _bg = "#070809" if _theme == "dark" else "#F6F5F2"
    st.markdown(f"<style>{LANDING_CSS} .stApp {{ background: {_bg}; }}</style>",
                unsafe_allow_html=True)
    # 开屏页右上角切的深浅色另存一份（ui_theme）：进入助手后开屏页组件不再渲染，它的组件状态会被 Streamlit 清掉
    landing = landing_surface({"theme": _theme}, on_theme=lambda: keep_theme("landing"))
    # 顶栏「评测」：去评测页（?eval=1）
    if getattr(landing, "go", None) == "eval":
        st.query_params["eval"] = "1"
        st.rerun()
    if getattr(landing, "enter", None):
        st.session_state["entered"] = True
        # 「这样问」：进入助手后直接发出那句话。数据来自浏览器，只收短字符串，照常走提问计次
        _ask = landing.enter.get("ask") if isinstance(landing.enter, dict) else None
        if isinstance(_ask, str) and 0 < len(_ask.strip()) <= 60:
            st.session_state["pending_ask"] = _ask.strip()
        st.query_params["chat"] = "1"
        st.rerun()
    st.stop()
st.session_state["entered"] = True

# ---------- 侧边栏：① 上传与文档选择 ----------
# 选择状态只有一个来源：st.session_state["doc_id"]。
# 上传只在“新文件”时把选择切过去一次，之后用户可以自由切换到任意已上传的年报。
with st.sidebar:
    up = st.file_uploader("上传年报或季报 PDF", type=["pdf"],
                          disabled=config.PUBLIC_DEMO
                          and uploads_used() >= config.MAX_UPLOADS_PER_SESSION)
    if up is not None and st.session_state.get("last_upload_id") != up.file_id:
        st.session_state.last_upload_id = up.file_id
        new_id, err = ingest(up.getvalue(), up.name, check_limits=config.PUBLIC_DEMO)
        st.session_state.upload_error = err
        if new_id:
            visible = visible_ids()
            if visible is not None:
                visible.add(new_id)
            st.session_state.doc_id = new_id
    if st.session_state.get("upload_error"):
        st.error(st.session_state.upload_error)
    if config.PUBLIC_DEMO:
        st.caption(f"在线演示：单个文件不超过 40 MB、{config.MAX_PAGES} 页，每个会话最多上传 "
                   f"{config.MAX_UPLOADS_PER_SESSION} 份。上传的文件会交给服务器处理，不保证长期保存，"
                   "请勿上传敏感资料。")

    # 第 1 阶段：粘贴巨潮链接 → 服务器下载 → 复用同一条 ingest 链路。
    # 只接受白名单域名；下载失败只报错，不影响已导入的文档。
    if config.REPORT_FETCH_ENABLED:
        with st.expander("手动添加年报或季报（粘贴巨潮链接）"):
            st.caption("自动查找失败时，可以在这里粘贴巨潮 PDF 链接；只支持 cninfo 的地址。")
            report_url = st.text_input(
                "巨潮 PDF 链接", key="report_url",
                placeholder="https://static.cninfo.com.cn/finalpage/...PDF")
            at_limit = (config.PUBLIC_DEMO
                        and uploads_used() >= config.MAX_UPLOADS_PER_SESSION)
            if st.button("下载并导入", key="report_import", use_container_width=True,
                         disabled=at_limit):
                st.session_state.report_error = ""
                try:
                    with st.spinner("正在下载并校验 PDF…"):
                        data, fname = download_pdf(report_url)
                    new_id, err = ingest(data, fname, check_limits=config.PUBLIC_DEMO,
                                         source_url=report_url.strip(),
                                         source_site=source_site(report_url))
                    st.session_state.report_error = err
                    if new_id:
                        visible = visible_ids()
                        if visible is not None:
                            visible.add(new_id)
                        st.session_state.doc_id = new_id
                        st.rerun()
                except Exception as e:
                    st.session_state.report_error = str(e)
            if at_limit:
                st.caption(f"每个会话最多导入 {config.MAX_UPLOADS_PER_SESSION} 份。")
            if st.session_state.get("report_error"):
                st.error(st.session_state.report_error)

    # 旧版本索引自动用保存的原 PDF 重建，无需重新上传
    for d in list_documents(visible_ids()):
        if d.get("index_version", 1) != config.INDEX_VERSION:
            ingest((doc_dir(d["doc_id"]) / "source.pdf").read_bytes(), d["name"],
                   demo=d.get("demo", False),
                   source_url=d.get("source_url", ""),
                   source_site=d.get("source_site", ""),
                   company=d.get("company", ""),
                   year=d.get("year", ""),
                   period=d.get("period", ""))

    docs = {d["doc_id"]: d for d in list_documents(visible_ids())}
    if docs:
        if st.session_state.get("doc_id") not in docs:
            st.session_state.doc_id = next(iter(docs))
        # 每家公司一行；年份和报告期在对话框顶栏的下拉里切换。
        # 点公司回到上次在这家公司看的那份报告（last_doc 按对话键记）。
        last_doc = st.session_state.setdefault("last_doc", {})
        cur_key = turn_core.conversation_key(docs[st.session_state.doc_id])
        last_doc[cur_key] = st.session_state.doc_id
        st.markdown("**公司**")
        for g in turn_core.company_groups(docs.values()):
            if st.button(g["label"], key=f"co-{g['key']}", use_container_width=True,
                         type="primary" if g["key"] == cur_key else "secondary"):
                target_doc = turn_core.pick_for_company(g["docs"], last_doc.get(g["key"]))
                st.session_state.doc_id = target_doc["doc_id"]
                st.session_state.pop("page", None)
                st.rerun()
        cur = docs.get(st.session_state.get("doc_id"))
        if cur and cur.get("source_url"):
            st.caption(f"来源：{cur.get('source_site') or '外部链接'}")

    # 大模型 Key：公开模式下访客可以填自己的，不计入免费额度
    user_key = ""
    if config.PUBLIC_DEMO:
        st.divider()
        user_key = st.text_input("使用自己的 DeepSeek Key（可选）", type="password", key="user_key",
                                 help="填写后不再占用免费额度。Key 只保存在当前会话内存里，不会写入磁盘。"
                                 ).strip()
    elif not config.DEEPSEEK_API_KEY:
        st.warning("未检测到 DEEPSEEK_API_KEY，请在 .env 中配置。")
    st.caption("提问优先检索上方选中的报告；问到别的公司、别的年份或「三季度」「上半年」时，"
               "助手会先去巨潮找对应的年报或季报。同一家公司的年报和季报共用一段对话。")
    st.caption("若侧边栏显示异常，请关闭浏览器的“翻译此页”后刷新。")
    # 评测页（?eval=1）：每次上线前的评测结果。新标签页打开，不打断当前对话
    st.caption("[它答得准不准？看评测结果 →](?eval=1)")

if not docs:
    st.markdown("### 请先在左侧上传一份上市公司年报 PDF（文字版）")
    st.stop()

doc_id = st.session_state.doc_id
meta, retriever = get_retriever(doc_id, config.INDEX_VERSION, SOURCE_STAMP)
meta = dict(meta)
meta["doc_id"] = doc_id
# 对话按公司存放：同一家公司不同年度的年报共用一段对话（见 turn_core.conversation_key），
# 问完 2025 再问「那 2024 年呢」时对话接着往下走。每条历史自己记着依据的是哪份年报。
conv_key = turn_core.conversation_key(meta)
history = st.session_state.setdefault("histories", {}).setdefault(conv_key, [])

# 进行中的回合。切换年报就丢弃，避免状态串到另一份文档上 ——
# 但如果扩展分析阶段的首轮回答已经产出，用户已经看到了，先把那半段落进原文档所在的对话。
turn, orphan = turn_core.adopt_turn(st.session_state.get("turn"), doc_id)
if orphan is not None:
    old_id, entry = orphan
    old_key = turn_core.conversation_key(docs.get(old_id) or {"doc_id": old_id})
    st.session_state["histories"].setdefault(old_key, []).append(entry)
if turn is None:
    st.session_state.pop("turn", None)

theme = current_theme()
st.markdown(shell_style(theme), unsafe_allow_html=True)


def pick_client():
    """本次提问使用的大模型客户端：访客自己的 Key 优先，否则用站长配置的 Key。"""
    if user_key:
        return make_client(user_key)
    return get_client() if config.DEEPSEEK_API_KEY else None


def quota_hint() -> str:
    if not config.PUBLIC_DEMO:
        return ""
    if user_key:
        return "已使用你自己的 Key，提问不限次数"
    left = max(0, config.SESSION_LIMIT - st.session_state.get("used", 0))
    return f"本会话剩余免费提问 {left} / {config.SESSION_LIMIT} 次"


# ---------- 挂载对话面板 ----------
# 错误态不算「忙」，否则失败后用户会被卡住无法重新提问
busy = bool(turn) and turn.get("status") != "error"
page = st.session_state.get("page")
messages = payload.build_messages(history, turn, page=page,
                                  doc_labels={did: doc_label(d) for did, d in docs.items()})
# 浏览器里的对话存档：刷新后由组件送回来（restore），之后每次历史有变化就送一份新的让它存。
# 恢复完成之前不送存档 —— 否则空历史会先把浏览器里存着的对话覆盖掉。
persist = None
if st.session_state.get("restored"):
    sig = (conv_key, len(history), history[-1]["q"] if history else "")
    if st.session_state.get("persist_sig") != sig:
        persist = {"key": conv_key, "entries": chat_memory.pack(history)}
        st.session_state["persist_sig"] = sig

result = chat_surface(
    payload.build_data(
        doc=meta,
        messages=messages,
        theme=theme,
        busy=busy,
        warn=st.session_state.get("chat_warn", ""),
        hint=quota_hint(),
        model=config.LLM_MODEL,
        embed_model=config.EMBED_MODEL,
        reports=payload.report_menu(docs.values(), meta),
        persist=persist,
        restore_wanted=not st.session_state.get("restored"),
        style_switch=config.ANSWER_STYLE != "pro",
    ),
    key="chat",
    on_theme=lambda: keep_theme("chat"),
)

# 顶栏「首页」：回开屏页。对话、文档都在 session_state 里，再点「开始聊天」原样回来。
# 深浅色两页共用 ui_theme（见 current_theme），不用另带。生成中前端按钮是禁用的，这里再挡一次。
if getattr(result, "go", None) == "home" and not busy:
    st.session_state["entered"] = False
    st.query_params.pop("chat", None)
    st.rerun()


def fail(target: dict, msg: str) -> None:
    """把错误挂到进行中的那一轮上，让组件把它显示在对话流里，而不是整页报错。
    错误态不算「忙」，用户可以立刻重新提问。"""
    target["status"] = "error"
    target["error"] = msg
    st.session_state["turn"] = target


def refund(target: dict) -> None:
    """调用失败就把已经扣掉的额度退回去。"""
    if target.pop("counted", False):
        get_quota().refund()
        st.session_state.used = max(0, st.session_state.get("used", 1) - 1)


def finish_turn(entry: dict) -> None:
    """把一轮写进历史，并清掉进行中的回合。

    先清回合再写历史：每次读写 st.session_state 都是 Streamlit 的打断点（浏览器发来事件时在这里抛
    RerunException）。反过来写，打断落在两步之间会留下「已写进历史、回合还在」，下一轮再写一遍。"""
    st.session_state.pop("turn", None)
    history.append(entry)
    st.session_state["chat_warn"] = ""


# ---------- 第二期：分步读别的报告（rag/notes.py 的前置步骤） ----------
# 主报告以外最多再读 2 份。每份只为记笔记：主要会计数据够用就只用程序抽的数（不调模型）；
# 要的数据超出时，在那份报告上检索 + 只出表格的简短生成，核对过的格子记成笔记。
# 前置步骤失败不影响主回答，只在分步记录里记一笔。注意：这里只读 meta / history，不给它们赋值。

def prestep_text(target: dict, i: int) -> str:
    steps = target.get("also_read") or []
    if i >= len(steps):
        return "正在检索相关片段…"
    label = report_label(steps[i]["year"], steps[i]["period"])
    return f"第 {i + 1} 步：先读 {label}，记下要对比的数据…"


def run_prestep(target: dict) -> None:
    """执行第 pre_i 个前置步骤，结果写回 target；全部做完（或超时）就转回 retrieving。"""
    steps = target.get("also_read") or []
    i = int(target.get("pre_i") or 0)
    log = target.setdefault("steps_log", [])
    company = meta.get("company", "")
    main_label = f"{company} {report_label(meta.get('year', ''), period_of(meta))}".strip()

    def finish():
        log.append({"src": main_label, "n": 0, "status": "main"})
        target["pre_done"] = True
        # 主步骤从这里起算：复核回退只重跑主步骤，回退的时间预算按它估（rag/reviewer.decide）
        target["main_started"] = time.time()
        target["status"] = "retrieving"
        target.pop("step_text", None)

    if i >= len(steps):
        finish()
        return
    step = steps[i]
    label = f"{company} {report_label(step['year'], step['period'])}".strip()
    rec = {"src": label, "n": 0, "status": "ok"}
    target["pre_i"] = i + 1
    target["step_text"] = prestep_text(target, i + 1)
    if time.time() - target.get("started", time.time()) > config.PRESTEP_BUDGET:
        # 超时：这一份和后面的都不读了，主回答照常
        log.extend({"src": f"{company} {report_label(x['year'], x['period'])}".strip(), "n": 0,
                    "status": "timeout"} for x in steps[i:])
        finish()
        return
    try:
        plan = turn_core.plan_resolution(
            {"company": company, "year": step["year"], "period": step["period"]},
            current_company=company, current_year=meta.get("year", ""),
            current_period=period_of(meta), docs=list_documents(visible_ids()))
        sdoc = None
        if plan["action"] == "current":
            rec["status"] = "same"                      # 就是主报告本身，不用另读
        elif plan["action"] == "switch":
            sdoc = plan["doc"]["doc_id"]
        elif plan["action"] == "import":
            if config.PUBLIC_DEMO and uploads_used() >= config.MAX_UPLOADS_PER_SESSION:
                rec["status"] = "quota"
            else:
                sdoc, label = import_report(company, step["year"], step["period"])
                rec["src"] = label
        if sdoc and fact_notes.already_noted(history, sdoc, step.get("need") or []):
            rec["status"] = "have"                      # 前几轮已经有这份报告要的数，as_hits 会带上
            sdoc = None
        if sdoc:
            client = pick_client()
            _, sret = get_retriever(sdoc, config.INDEX_VERSION, SOURCE_STAMP)
            got = fact_notes.read_step(
                sdoc, rec["src"],
                fact_notes.prestep_question(company, report_label(step["year"], step["period"]),
                                            step.get("need") or []),
                step.get("need") or [],
                fin=get_financials(sdoc, SOURCE_STAMP),
                search=lambda q: sret.search_with_queries(q, client=client, use_llm=False)[0],
                gen=lambda q, h: "".join(generate(q, h, client=client, company=company,
                                                  year=str(step["year"]), period=step["period"],
                                                  focus=fact_notes.PRESTEP_FOCUS, style="pro")),
                check=unverified_in_tables)
            target["step_notes"] = (target.get("step_notes") or []) + got
            rec["n"] = len(got)
    except Exception as e:
        rec["status"] = "error"
        target["prestep_error"] = f"{type(e).__name__}: {e}"
    log.append(rec)
    if i + 1 >= len(steps):
        finish()


# ---------- 财报体检：季报上用哪份年报做底稿 ----------

def health_base() -> tuple[str | None, str, str]:
    """当前季报之前的那份年报（2026 三季报 → 2025 年报）：(doc_id, 报告名, 给用户的说明)。

    本地有就用；没有就去巨潮导入（和前置步骤一样计入每个会话的导入上限）。拿不到时 doc_id 为 None，
    说明写清原因 —— 体检照样做，只是只有季报能查的几项。注意：这里只读 meta，不给它赋值。
    """
    company = meta.get("company", "")
    year = int(meta["year"]) - 1 if str(meta.get("year", "")).isdigit() else None
    if not company or year is None:
        return None, "", ""
    label = f"{company} {report_label(year, FY)}"
    plan = turn_core.plan_resolution({"company": company, "year": year, "period": FY},
                                     current_company=company, current_year=meta.get("year", ""),
                                     current_period=period_of(meta), docs=list_documents(visible_ids()))
    if plan["action"] == "switch":
        return plan["doc"]["doc_id"], label, ""
    if config.PUBLIC_DEMO and uploads_used() >= config.MAX_UPLOADS_PER_SESSION:
        return None, label, (f"本会话的报告导入次数已用完，没能取来《{label}》做底稿，"
                             "这次只做了季报能查的几项。")
    try:
        new_id, label = import_report(company, year, FY)
    except Exception as e:
        return None, label, f"没能从巨潮取到《{label}》（{e}），这次只做了季报能查的几项。"
    return new_id, label, ""


# ---------- 第三期：复核要求去别的报告补检索 ----------
# 复核器只能从这几份里挑（rag/reviewer.normalize 只认给它看过的名字）：本地已有的同一公司别的报告，新的在前。
# 不去巨潮下载 —— 回退要快，而且没读过的报告复核器也说不准答案在不在里面。

def other_reports() -> dict[str, str]:
    """{「2026 半年报」: doc_id}：当前公司本地已有的别的报告（rag/notes.other_reports）。"""
    return fact_notes.other_reports(list_documents(visible_ids()), meta, doc_id)


def supplement_hits(target: dict, sdoc: str, client) -> tuple[list[dict], list[str]]:
    """在别的报告上用复核给的检索词找，前 RETRY_OTHER_TOP_K 段原文（rag/notes.as_supplement）
    接在上一轮的片段后面。主报告的片段不重找，上一轮的照留。"""
    src = f"{meta.get('company', '')} {target['retry_report']}".strip()
    _, sret = get_retriever(sdoc, config.INDEX_VERSION, SOURCE_STAMP)
    found, used = sret.search_with_queries(target["q"], client=client, use_llm=False,
                                           queries=target.pop("retry_queries", None) or None)
    extra = fact_notes.as_supplement(found, sdoc, src, config.RETRY_OTHER_TOP_K)
    base = [h for h in target.get("hits") or [] if not h.get("extra")]
    queries = list(dict.fromkeys(list(target.get("queries") or []) + list(used)))
    return base + extra, queries


def advance_turn() -> None:
    """回合状态机：resolving -> retrieving -> (history) -> generating -> analyzing / searching -> done。

    关键是「先挂载组件、再做耗时工作」：组件挂载的增量会在几十毫秒内送达浏览器并绘制，
    之后的阻塞调用（检索、生成）期间用户看到的就是刚挂上去的那一帧。已实测成立。

    analyzing 是后加的第二步：首轮回答产出后不直接收尾，而是再问一次模型「为什么」，
    把结果作为独立一段追加。**首轮回答在这一步就已经显示出来了** ——
    组件对 analyzing 状态会先渲染已有的正文，再挂「正在扩展分析」的指示，
    所以用户不必等第二次调用跑完才看到东西。
    """
    target = st.session_state.get("turn")
    if not target or target.get("status") == "error":
        return

    if target["status"] == "resolving":
        # 先判断这一问是不是在问另一家公司/另一个年度；是的话先把年报找来。
        # 识别失败或没提到公司 → 用当前文档，不额外联网。
        client = pick_client()
        if client is None:
            refund(target)
            fail(target, "未配置 DeepSeek Key，无法回答。请在 .env 或 Secrets 中配置，"
                         "或在左侧填写自己的 Key。")
            st.rerun()
        # agent1 规划器（rag/planner.py）：一次调用拿到意图、目标报告、要点和检索词。
        # 失败或关掉时退回旧流程（模型认公司年份 + 规则），形状一样。
        docs_here = list_documents(visible_ids())
        ctx = chat_memory.recent_context(history, config.FOLLOWUP_HISTORY_TURNS)
        understood = None
        if config.AGENT_PLANNER:
            understood = planner.understand(target["q"], client=client, history=ctx,
                                            current=meta, docs=docs_here,
                                            hint=target.get("replan_hint", ""))
        if understood is None:
            understood = planner.fallback(target["q"], client=client, history=ctx, current=meta)
        if target.get("kind") in ("health", "drivers", "statements"):
            # 体检、几件大事、三表速读只用规划器认公司和报告（「给七匹狼做个体检」要先切过去）；
            # 它判成反问、超范围、行业动态都不算 —— 对当前报告总是做得了。
            # intent 照记：没指定哪一期时优先用年报（turn_core.plan_for）
            understood = dict(understood, intent=target["kind"])
        target["plan"] = understood
        target["target"] = understood["target"]
        target["understood"] = planner.describe(understood)
        # 第二期：主报告以外要先读的报告（rag/planner._also_read 已做过代码兜底）。体检、几件大事不分步
        if understood.get("also_read") and not target.get("kind"):
            target["also_read"] = understood["also_read"]
        if understood["intent"] == "industry":
            target["kind"] = "industry"
        plan = turn_core.plan_for(
            understood, current_company=meta.get("company", ""),
            current_year=meta.get("year", ""),
            current_period=period_of(meta), docs=docs_here)

        if plan["action"] in ("clarify", "out_of_scope"):
            # 反问 / 超出范围：不检索、不生成，规划器给的一句话就是回答。没花生成的钱，退还额度。
            refund(target)
            if plan["action"] == "clarify":
                c = understood["clarify"]
                entry = turn_core.quick_entry(target, c["question"], "clarify", c["options"])
            else:
                entry = turn_core.quick_entry(
                    target, understood["reply"] or "这里只能回答上市公司定期报告里的内容。", "refuse")
            finish_turn(entry)
            st.rerun()

        if plan["action"] == "multiple":
            refund(target)
            fail(target, "一次只能查一家公司。请先问其中一家，或分别上传后再对比。")
            st.rerun()

        if plan["action"] == "current":
            # 当前文档就是目标：不 rerun，继续走下面的检索分支。
            target["status"] = "retrieving"
            st.session_state["turn"] = target

        elif plan["action"] == "switch":
            d = plan["doc"]
            st.session_state.doc_id = d["doc_id"]
            target["doc_id"] = d["doc_id"]
            target["notice"] = f"已切换到《{doc_label(d)}》，基于该文档回答"
            target["status"] = "retrieving"
            st.session_state.pop("page", None)
            st.session_state["turn"] = target
            st.rerun()

        else:
            # 报告期还没结束的报告（2026 年 11 月问「2026 年报」）不可能已经披露：直接说明，不去巨潮白找一趟再报「没有找到」
            early = not_out_yet(int(plan["year"]), plan["period"]) if str(plan.get("year") or "").isdigit() else ""
            if early:
                refund(target)
                finish_turn(turn_core.quick_entry(target, early, "refuse"))
                st.rerun()
            if config.PUBLIC_DEMO and uploads_used() \
                    >= config.MAX_UPLOADS_PER_SESSION:
                refund(target)
                fail(target, f"本会话最多导入 {config.MAX_UPLOADS_PER_SESSION} 份报告。"
                             "请切换到已有文档，或手动上传/粘贴链接。")
                st.rerun()
            try:
                new_id, label = import_report(plan["company"], plan["year"], plan["period"],
                                              newer=plan.get("newer"))
            except Exception as e:
                refund(target)
                fail(target, f"{e}。可以在左侧上传 PDF，或粘贴巨潮链接。")
                st.rerun()
            st.session_state.doc_id = new_id
            target["doc_id"] = new_id
            target["notice"] = f"已自动导入《{label}》，基于该文档回答"
            target["status"] = "retrieving"
            st.session_state.pop("page", None)
            st.session_state["turn"] = target
            st.rerun()

    if target["status"] == "retrieving" and target.get("kind") == "health":
        # 体检：不检索、不生成。卡片和正文都由程序按披露写（ui/turn.health_entry）
        try:
            hc = get_health(doc_id, SOURCE_STAMP)
        except Exception as e:
            hc = None
            target["health_error"] = f"{type(e).__name__}: {e}"
        if not hc:
            refund(target)
            fail(target, "这份报告没能完成体检（读取原文失败）。可以换一份报告再试。")
            st.rerun()
        label = f"{meta.get('company', '')} {report_label(meta.get('year', ''), period_of(meta))}".strip()
        note = ""
        # 财报体检：季报上以那之前的年报 9 项为底，季报判得了的几项用季报更新（rag/healthcheck.merge_quarter）。
        # 用户点名要季报那一期（「三季报体检」）就只做季报
        named = (target.get("target") or {}).get("period") == period_of(meta)
        if period_of(meta) != FY and not named:
            base_id, base_label, note = health_base()
            base_hc = None
            if base_id:
                try:
                    base_hc = get_health(base_id, SOURCE_STAMP)
                except Exception as e:
                    target["health_error"] = f"{type(e).__name__}: {e}"
                if not base_hc:
                    note = f"没能读取《{base_label}》，这次只做了季报能查的几项。"
            if base_hc:
                hc = merge_quarter(base_hc, hc, {"doc": base_id, "label": base_label},
                                   {"doc": doc_id, "label": label})
        finish_turn(turn_core.health_entry(target, hc, label, note))
        st.rerun()

    if target["status"] == "retrieving" and target.get("kind") == "statements":
        # 三表速读：不检索、不生成。卡片和正文都由程序按三张合并报表写（ui/turn.statements_entry）
        try:
            stmts = get_statements(doc_id, SOURCE_STAMP)
        except Exception as e:
            stmts = None
            target["statements_error"] = f"{type(e).__name__}: {e}"
        if not stmts:
            refund(target)
            fail(target, "这份报告没能读出三张报表（读取原文失败）。可以换一份报告再试。")
            st.rerun()
        label = f"{meta.get('company', '')} {report_label(meta.get('year', ''), period_of(meta))}".strip()
        c = three_statements.card(stmts, period_of(meta), meta.get("year", ""))
        finish_turn(turn_core.statements_entry(target, c, label))
        st.rerun()

    if target["status"] == "retrieving" and target.get("kind") == "drivers":
        # 几件大事第一段：从年报里找因素（一次模型调用，按报告缓存在 drivers.json）
        client = pick_client()
        if client is None:
            fail(target, "未配置 DeepSeek Key，无法分析。请在 .env 或 Secrets 中配置，或在左侧填写自己的 Key。")
            st.rerun()
        try:
            found = key_drivers.load_or_build(doc_id, client=client)
        except Exception as e:
            found = None
            target["drivers_error"] = f"{type(e).__name__}: {e}"
        if not found:
            refund(target)
            fail(target, "这次没能从报告里找出关键因素（模型调用失败），请稍后再试。")
            st.rerun()
        target["found"] = found
        target["status"] = "latest"
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] == "latest" and target.get("kind") == "drivers":
        # 第二段：官方行情 + 联网动态（按天缓存在 drivers_now_<日期>.json），再按固定公式估算
        client = pick_client()
        found = target.get("found") or {}
        try:
            latest = key_drivers.load_latest(
                doc_id, found, client=client,
                api_key=getattr(client, "api_key", "") or config.DEEPSEEK_API_KEY,
                company=meta.get("company", ""))
        except Exception as e:
            latest = {"date": "", "drivers": found.get("drivers") or [], "web_sources": []}
            target["latest_error"] = f"{type(e).__name__}: {e}"
        fin = get_financials(doc_id, SOURCE_STAMP)
        label = f"{meta.get('company', '')} {report_label(meta.get('year', ''), period_of(meta))}".strip()
        done = {k: v for k, v in target.items() if k != "found"}
        finish_turn(turn_core.drivers_entry(done, key_drivers.card(found, latest, fin), label,
                                            latest.get("web_sources") or []))
        st.rerun()

    if target["status"] == "retrieving" and target.get("also_read") and not target.get("pre_done") \
            and target.get("kind") != "industry":
        # 第二期：先把别的报告的数记成笔记，再在主报告上检索回答。每读一份 rerun 一次，界面状态跟着走
        target["status"] = "prestep"
        target["pre_i"] = 0
        target["step_text"] = prestep_text(target, 0)
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] == "prestep":
        run_prestep(target)
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] == "retrieving":
        client = pick_client()
        if client is None:
            fail(target, "未配置 DeepSeek Key，无法回答。请在 .env 或 Secrets 中配置，"
                         "或在左侧填写自己的 Key。")
            st.rerun()
        # 带最近几轮的问题给改写，追问（「那去年呢」）里的指代才消解得掉。
        # 只传问题不传回答：上一轮回答里可能有算错或臆测的数字，带进检索词会污染检索。
        recent = [t["q"] for t in history[-config.FOLLOWUP_HISTORY_TURNS:]]
        # 生成侧另外带上一轮回答的结论摘要（去掉数字），检索侧仍然只用问题
        target["recent_ctx"] = chat_memory.recent_context(history,
                                                          config.FOLLOWUP_HISTORY_TURNS)
        # 存到回合上：生成阶段也要用它。检索侧把这几个问题喂给改写、
        # 生成侧则用它消解「那去年呢」里的指代 —— 两边用的是同一份。
        target["recent"] = recent
        diag: dict = {}
        # 复核要求去别的报告补检索（第三期）：名字对不上本地报告时退回在主报告里补检索
        other = other_reports().get(target.get("retry_report") or "")
        try:
            if other:
                hits, queries = supplement_hits(target, other, client)
            elif target.get("kind") == "industry":
                # 行业动态：从报告里找「所处行业、主要产品、原材料、竞争对手」，
                # 给联网检索定方向。问题本身是「最新动态」，拿它去检索年报找不到这些。
                hits, queries = retriever.search_with_queries(
                    INDUSTRY_QUERY, client=client, use_llm=False, diag=diag)
            else:
                # 规划器给了检索词就直接用（不再调一次改写）；退回旧流程时照旧改写
                pl = target.get("plan") or {}
                # 复核说要补检索时，用它给的检索词再找一遍；已经答到的要点的旧片段留着（ui/turn.answered_hits）
                retry = target.pop("retry_queries", None)
                keep = (turn_core.answered_hits(target.get("hits"), target.get("review_last"),
                                                config.MAX_POINTS_K - config.TOP_K // 2) if retry else [])
                hits, queries = retriever.search_with_queries(
                    target["q"], client=client, use_llm=config.LLM_REWRITE,
                    history=recent or None, diag=diag,
                    queries=retry or pl.get("queries") or None,
                    restated=pl.get("restated", ""),
                    # 按要点分别检索（一问多项）：每个要点先各取几段（rag/retriever.select_by_points）
                    points=None if retry else pl.get("points"),
                    k=config.TOP_K if not keep else max(config.TOP_K // 2, config.MAX_POINTS_K - len(keep)),
                )
                if keep:
                    ids = {h.get("chunk_id") for h in keep}
                    hits = keep + [h for h in hits if h.get("chunk_id") not in ids]
                if diag.get("points"):
                    target["by_points"] = diag["points"]
        except Exception as e:
            refund(target)
            fail(target, f"检索失败：{e}")
            st.rerun()
        # 改写退化（超时 / JSON 截断）会静默变成规则改写，追问就失效了。
        # 记在回合上，便于排查，也免得它无声无息。
        target["rewrite"] = diag.get("rewrite", "unused")
        if diag.get("rewrite") == "fallback":
            target["rewrite_error"] = diag.get("rewrite_error", "")
        # 事实笔记：前几轮在别的报告里核对过的数字，接在检索片段后面编号（rag/notes.py）。
        # 行业动态的片段只用来给联网检索定方向，不带
        # 补检索去了别的报告时，上一轮的片段（含笔记）已经原样带上了
        if target.get("kind") != "industry" and not other:
            hits = hits + fact_notes.as_hits(history, doc_id, extra=target.get("step_notes"))
            hits = attach_history(hits, target.get("hist_hit"))
            hits = attach_quarters(hits, target.get("quarter_hit"))
            # 三表片段：问到报表科目、指标，或上一轮是三表速读（追问）时接上核对过的三张表（rag/statements.as_hits）。
            # 存在回合上：复核回退重新检索时照样接；重新理解换了报告就按新报告重做。失败只记下来，不拖垮回答
            if "stmt_hits" not in target or any(h.get("doc_id") != doc_id for h in target["stmt_hits"]):
                target["stmt_hits"] = []
                if three_statements.wants_items(target["q"], history[-1].get("kind") if history else None):
                    try:
                        target["stmt_hits"] = three_statements.as_hits(
                            get_statements(doc_id, SOURCE_STAMP), doc_id,
                            f"{meta.get('company', '')} {report_label(meta.get('year', ''), period_of(meta))}".strip(),
                            period_of(meta), meta.get("year", ""))
                    except Exception as e:
                        target["stmt_error"] = f"{type(e).__name__}: {e}"
            hits = three_statements.attach(hits, target["stmt_hits"])
        target["hits"] = hits
        target["queries"] = queries
        target["src_label"] = f"{meta.get('company', '')} {report_label(meta.get('year', ''), period_of(meta))}".strip()
        # 问到营收、利润、现金流这类方向时，附上程序抽取的指标卡。
        # 抽取失败只记在回合上 —— 卡片是锦上添花，不能拖垮回答本身。
        target["status"] = "generating"
        # 出不出卡、要不要拉长趋势：规划器判断过就听它的（它见过上下文），否则用关键词规则
        pl = target.get("plan") or {}
        want_card = (pl["card"] if pl.get("source") == "planner"
                     else wants_card(target["q"], previous=recent))
        want_trend = pl.get("intent") == "trend" or wants_trend(target["q"], previous=recent)
        # 复核回退后的第二轮检索：卡片第一轮已经算过，不再重抽（也不再拉一次往年年报）
        if target.get("kind") != "industry" and want_card and not target.get("kpis"):
            try:
                fin = get_financials(doc_id, SOURCE_STAMP)
                card = build_card(fin)
            except Exception as e:
                fin, card = None, None
                target["card_error"] = f"{type(e).__name__}: {e}"
            if card:
                target.update(card)
                # 卡片上的数字记成笔记：只用这份报告自己的 fin（六年趋势拼进来的往年数据页码不在这份里）
                target["fin_notes"] = fact_notes.from_financials(fin, doc_id, target["src_label"])
                # 问趋势时再抽一份往年年报，把卡片拉长到六年（单独一轮，好显示状态文案）；
                # 季报上问趋势、或点名要季度数据时，改拼最近 8 季的单季序列
                annual = (fin.get("period") or FY) == FY
                if wants_quarters(target["q"], previous=recent) or (want_trend and not annual):
                    target["status"] = "quarters"
                elif annual and want_trend:
                    target["status"] = "history"
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] == "history":
        # 拉长到六年是锦上添花：任何失败都退回三年卡，回答照常生成。
        # 注意别给 meta / doc_id 赋值：它们是模块级变量，函数里一赋值就整函数变局部
        company = meta.get("company", "")
        fin = get_financials(doc_id, SOURCE_STAMP)
        year = older_year(fin or {})
        if fin and year and company:
            used = st.session_state.get("history_fetches", 0)
            try:
                older, fetched = load_older(
                    company, year, list_documents(None),
                    allow_fetch=used < config.HISTORY_FETCHES_PER_SESSION)
                if fetched:
                    st.session_state["history_fetches"] = used + 1
                merged = merge(fin, older)
                card = build_card(merged)
                if card:
                    target.update(card)
                # 往年数字进回答：接得上（merge 真的拼了）才加片段。存在回合上：复核回退重新检索时还要接上
                if merged is not fin:
                    target["hist_hit"] = history_hit(older, company)
                    target["hits"] = attach_history(target["hits"], target["hist_hit"])
            except Exception as e:
                target["history_error"] = f"{type(e).__name__}: {e}"
        target["status"] = "generating"
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] == "quarters":
        # 单季折线也是锦上添花：失败就留着原来的卡片。缺的季报只抽表，会话额度单独计（rag/quarters.py）
        fin = get_financials(doc_id, SOURCE_STAMP)
        company = meta.get("company", "")
        if fin and fin.get("years") and company:
            used = st.session_state.get("quarter_fetches", 0)
            try:
                series, fetched, errors = build_quarters(
                    company, int(fin["years"][0]), fin.get("period") or FY, list_documents(None),
                    budget=max(0, config.QUARTER_FETCHES_PER_SESSION - used), load=load_report)
                st.session_state["quarter_fetches"] = used + fetched
                card = build_card(fin, quarters=series)
                if card:
                    target.update(card)
                # 单季数也进回答；存在回合上，复核回退重新检索时还要接上
                target["quarter_hit"] = quarter_hit(series, doc_id, fin.get("page"), company)
                target["hits"] = attach_quarters(target["hits"], target["quarter_hit"])
                if errors:
                    target["quarters_error"] = "；".join(errors)[:300]
            except Exception as e:
                target["quarters_error"] = f"{type(e).__name__}: {e}"
        target["status"] = "generating"
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] in ("generating", "reviewing", "analyzing", "searching"):
        # 决策（要不要扩展、什么时候收尾、失败怎么办）在 ui/turn.py 里，
        # 是纯函数、有测试。这里只负责接真实模型和 session_state。

        def _gen(q, hits, *, client, company, year, history):
            # 问题理解 + 上一版被复核指出的问题（回退重答时才有）
            focus = (planner.focus_block(target.get("plan"))
                     + reviewer.feedback_block(target.get("review_last")))
            parts = []
            for piece in generate(q, hits, client=client, company=company,
                                  year=year, history=history, period=period_of(meta),
                                  focus=focus, style=target.get("style", "pro")):
                parts.append(piece)
                # 打断点：浏览器点了「停止生成」等事件时，Streamlit 在读 session_state 时抛 RerunException，
                # 不用等整段生成完（约 20–40 秒）。回合状态还是 generating，没被别的事件停掉就从头再生成。
                st.session_state.get("turn")
            return "".join(parts)

        def _rev(q, answer, hits, *, client):
            # 跨口径检查用的带口径的数：主报告的主要会计数据 + 别的报告的事实笔记（rag/caliber.py）
            try:
                main_notes = fact_notes.from_financials(get_financials(doc_id, SOURCE_STAMP), doc_id,
                                                        target.get("src_label", ""))
            except Exception:
                main_notes = []
            facts = caliber.facts(main_notes + caliber.note_lines(hits))
            return reviewer.review(q, answer, hits, client=client, plan=target.get("plan"),
                                   facts=facts, reports=list(other_reports()))

        def _ana(q, hits, first, *, client, company, year, history):
            diag: dict = {}
            ext = analyze(q, hits, first, client=client, company=company, year=year,
                          diag=diag, history=history, period=period_of(meta))
            return ext, diag

        def _web(q, first, *, client, company, year, history):
            diag: dict = {}
            text, sources, diag = answer_with_tools(
                q, first, client=client,
                api_key=getattr(client, "api_key", "") or config.DEEPSEEK_API_KEY,
                company=company, year=year, diag=diag, period=period_of(meta),
            )
            return text, sources, diag

        def _ind(q, hits, *, client, company, year, history):
            diag: dict = {}
            return industry_brief(
                q, hits, client=client,
                api_key=getattr(client, "api_key", "") or config.DEEPSEEK_API_KEY,
                company=company, year=year, period=period_of(meta), diag=diag,
            )

        new_turn, entry, error = turn_core.step(
            target, gen=_gen, ana=_ana, web=_web, ind=_ind, client=pick_client(),
            company=meta.get("company", ""), year=meta.get("year", ""),
            recent=target.get("recent_ctx") or target.get("recent"),
            rev=_rev if (config.AGENT_REVIEW and target.get("kind") != "industry") else None,
        )
        if error:
            refund(target)
            fail(target, error)
            st.rerun()
        if entry is not None:
            finish_turn(entry)
            st.rerun()
        # 状态变了（generating → analyzing）：存回去，下一轮接着跑。
        target.update(new_turn)
        st.session_state["turn"] = target
        st.rerun()


# ---------- 来自组件的事件 ----------
# 新对话：清掉当前这家公司的对话（各年度共用一段），保留文档和索引；
# 生成中的一轮如果还没产出回答，退还额度。
if getattr(result, "new_chat", None):
    pending = st.session_state.get("turn")
    if pending and pending.get("counted") and not pending.get("a"):
        refund(pending)
    st.session_state.pop("turn", None)
    st.session_state["histories"][conv_key] = []
    st.session_state.pop("page", None)
    st.session_state["chat_warn"] = ""
    st.rerun()

# 停止生成：首轮回答已经出来就留下（标「已停止」），还没出就整轮丢掉、退还额度（前端把问题放回输入框）。
# 生成循环里有打断点，点下去一般一秒内生效；检索、复核、联网这类整段调用要等当前这一步返回。
if getattr(result, "stop", None) and busy:
    pending = st.session_state.get("turn")
    entry = turn_core.stop_turn(pending)
    if entry is None and pending:
        refund(pending)
    st.session_state.pop("turn", None)
    if entry is not None:
        history.append(entry)
    st.rerun()


def start_question(text: str, style: str | None = None) -> None:
    """开始一轮提问：聊天框里发的、开屏页「这样问」带过来的都走这里，计次规则一样。

    style 是界面上「通俗 / 专业」的选择（浏览器传来，不可信）：resolve_style 只认两个值，
    回滚开关 ANSWER_STYLE=pro 时一律按专业版。"""
    if text:
        count_it = config.PUBLIC_DEMO and not user_key
        if count_it:
            ok, why = check_and_consume(st.session_state.get("used", 0), config.SESSION_LIMIT,
                                        get_quota())
            if not ok:
                st.session_state["chat_warn"] = (
                    f"本会话的 {config.SESSION_LIMIT} 次免费提问已用完。"
                    "在左侧填写自己的 DeepSeek Key 可以继续提问。"
                    if why == "session" else
                    "今天的免费额度已被用完，明天再来，或在左侧填写自己的 DeepSeek Key。"
                )
                st.rerun()
            st.session_state.used = st.session_state.get("used", 0) + 1
        st.session_state["page"] = None
        st.session_state["chat_warn"] = ""
        st.session_state["turn"] = {
            "doc_id": doc_id, "q": text,
            "status": "resolving" if config.AUTO_IMPORT_ENABLED else "retrieving",
            "counted": count_it,
            "style": resolve_style(style),
            # 读秒：界面显示「已思考 n 秒」，完成后记成这一轮的用时（ui/turn.py:history_entry）
            "started": time.time(),
            # 行业动态走另一条路：联网检索成文，不做年报回答 + 扩展；
            # 体检、三表速读不检索不生成，程序直接出卡片（「行业风险」这种两边都沾的算行业动态；
            # 「三张表有什么风险」算体检）
            **({"kind": "industry"} if wants_industry(text)
               else {"kind": "drivers"} if key_drivers.wants_drivers(text)
               else {"kind": "health"} if wants_health(text)
               else {"kind": "statements"} if three_statements.wants_statements(text) else {}),
        }
        st.rerun()


if result.ask and not busy:
    asked = result.ask.get("text", "") if isinstance(result.ask, dict) else ""
    style = result.ask.get("style") if isinstance(result.ask, dict) else None
    start_question(asked.strip(), style if isinstance(style, str) else None)

# 开屏页「这样问」带过来的问题：进入助手后的第一次运行就发出去（一次性）
_pending_ask = st.session_state.pop("pending_ask", "")
if _pending_ask and not busy:
    start_question(_pending_ask)

# 刷新后浏览器送回来的对话存档。数据由访客控制：ui/memory.unpack 只收白名单字段，
# 文档 id 必须是服务器上真有的。只填空着的对话，不覆盖这次会话里已经有的。
if getattr(result, "restore", None) is not None and not st.session_state.get("restored"):
    st.session_state["restored"] = True
    known = {d["doc_id"] for d in list_documents(None)}
    convs, last = chat_memory.unpack(result.restore, known)
    hs = st.session_state["histories"]
    for k, entries in convs.items():
        if not hs.get(k):
            hs[k] = entries
    back = chat_memory.doc_ids_in(convs) | ({last} if last else set())
    visible = visible_ids()
    if visible is not None:
        visible.update(back)
        st.session_state.setdefault("restored_docs", set()).update(back)
    if last and not st.session_state.get("turn"):
        st.session_state.doc_id = last
    st.session_state.pop("persist_sig", None)
    st.rerun()

# 顶栏下拉里选了同一家公司的另一份报告：只换依据的文档，对话不断（同一个对话键）。
if getattr(result, "pick_doc", None) and isinstance(result.pick_doc, dict) and not busy:
    picked = str(result.pick_doc.get("id") or "")
    if picked in docs and picked != doc_id:
        st.session_state.doc_id = picked
        st.session_state.pop("page", None)
        st.rerun()

if result.open_page and isinstance(result.open_page, dict) and "p" in result.open_page:
    # 体检卡、几件大事、三表速读点开的原页：{p: 页码, turn: 历史里第几轮}。从那一轮依据的报告里渲染，
    # 页码必须在这份报告的页数之内（数据来自浏览器，按不可信输入处理）。
    try:
        p, k = int(result.open_page.get("p")), int(result.open_page.get("turn"))
    except (TypeError, ValueError):
        p, k = 0, -1
    entry = history[k] if 0 <= k < len(history) else {}
    # 财报体检里各项出自不同报告：按这一项（key）在服务端存的条目里查出是哪份
    hkey = str(result.open_page.get("key") or "")[:20]
    source_doc = turn_core.health_page_doc(entry, hkey, doc_id)
    total = int((docs.get(source_doc) or {}).get("pages") or 0)
    if entry.get("kind") in ("health", "drivers", "statements") and source_doc in docs and 1 <= p <= total:
        try:
            st.session_state["page"] = {"hp": p, "turn": k, "hk": hkey,
                                        "png": page_data_uri(source_doc, p, SOURCE_STAMP)}
        except Exception:
            st.session_state["page"] = None
        st.rerun()

elif result.open_page and isinstance(result.open_page, dict):
    n = int(result.open_page.get("n") or 1)
    last = history[-1] if history else {}
    hits = last.get("hits")
    # 原页要从**这条回答依据的那份年报**里渲染：同一段对话可能跨年度，
    # 用户问完 2025 再在侧边栏切到 2024，最后一条回答的引用仍然是 2025 年报的页码。
    source_doc = last.get("doc_id") or doc_id
    # 事实笔记片段来自别的报告，自带 doc_id
    if hits and 1 <= n <= len(hits) and hits[n - 1].get("doc_id"):
        source_doc = hits[n - 1]["doc_id"]
    if hits and 1 <= n <= len(hits) and is_hist_id(source_doc):
        # 往年年报片段：PDF 没留，翻的是下载时存下的表所在页截图（rag/history.page_png）
        png = hist_page_png(source_doc, int(hits[n - 1]["page_start"]))
        st.session_state["page"] = ({"n": n, "png": "data:image/png;base64,"
                                     + base64.b64encode(png).decode("ascii")} if png else None)
        st.rerun()
    elif hits and 1 <= n <= len(hits) and source_doc in docs:
        try:
            st.session_state["page"] = {
                "n": n, "png": page_data_uri(source_doc, int(hits[n - 1]["page_start"]), SOURCE_STAMP),
            }
        except Exception:
            st.session_state["page"] = None
        st.rerun()

advance_turn()

# ---------- 临时网络自检（?netcheck=1） ----------
# 验证部署环境（Streamlit Cloud）能否访问巨潮资讯。只有显式带参数时才渲染，
# 访客默认看不到；两个请求都是只读的，不落盘、不消耗 API 额度。
if str(st.query_params.get("netcheck", "")) == "1":
    from rag.netcheck import probe

    with st.sidebar:
        with st.expander("网络自检（临时）", expanded=True):
            st.caption("验证当前部署环境能否访问巨潮资讯；只发两个只读请求。")
            if st.button("开始检测", key="netcheck_btn"):
                st.json(probe())
