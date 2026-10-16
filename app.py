"""年报 AI 研究助手 —— Streamlit 界面（运行：streamlit run app.py）

界面分两块：
  · 左侧文档栏 = 原生 Streamlit 控件，只有它负责上传（40MB 的 PDF 走组件桥会在每次
    rerun 被重新序列化，不划算）。样式由 web/streamlit-shell.css 刷成设计稿的样子。
  · 主区对话面板 = ui/chat_component.py 里的 st.components.v2 自定义组件。

一次提问按「回合状态机」跑四轮脚本（见 advance_turn）。这么做是为了在整段呈现回答的前提下，
仍然给出真实的分阶段状态文案，而不是拿假计时器糊弄。
"""
import base64

import streamlit as st

from rag import config
from rag.chunker import chunk_pages
from rag.generator import generate, get_client, make_client
from rag.indexer import (build_index, cleanup_uploads, doc_dir, doc_id_of, ensure_demo,
                         is_current, list_documents, load_index)
from rag.limits import Quota, check_and_consume
from rag.pdf_parser import extract_doc_info, parse_pdf, render_page_png
from rag.retriever import Retriever
from ui import payload
from ui.chat_component import chat_surface

st.set_page_config(page_title="年报 AI 研究助手", page_icon="📊", layout="wide")

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
    """全站共用的每日额度计数器（进程内共享）。"""
    return Quota(config.DAILY_LIMIT)


@st.cache_resource(ttl=3600)
def startup() -> None:
    """进程启动时复制内置示例年报；公开模式下每小时清理过期的访客上传。"""
    ensure_demo()
    if config.PUBLIC_DEMO:
        cleanup_uploads(config.UPLOAD_TTL_HOURS)


startup()


@st.cache_resource(show_spinner="加载索引…")
def get_retriever(doc_id: str, index_version: int):
    meta, chunks, emb = load_index(doc_id)
    return meta, Retriever(chunks, emb, company=meta.get("company", ""), year=meta.get("year", ""))


@st.cache_data(show_spinner=False)
def render_page(doc_id: str, page_no: int) -> bytes:
    return render_page_png(doc_dir(doc_id) / "source.pdf", page_no)


@st.cache_data(show_spinner=False)
def page_data_uri(doc_id: str, page_no: int) -> str:
    """渲染原页并转成 data URI。base64 一起缓存，免得每次 rerun 重新编码。"""
    return "data:image/png;base64," + base64.b64encode(render_page(doc_id, page_no)).decode("ascii")


def ingest(data: bytes, name: str, demo: bool = False, check_limits: bool = False):
    """解析并建立索引。返回 (doc_id, 错误信息)；失败时 doc_id 为 None。"""
    did = doc_id_of(data)
    if is_current(did):
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
        chunks = chunk_pages(pages, config.CHUNK_SIZE, config.CHUNK_OVERLAP, info)
        st.write(f"{len(chunks)} 个片段。④ 向量化并建立索引（首次需下载模型）…")
        build_index(did, data, name, len(pages), chunks, company=info.company, year=info.year,
                    demo=demo)
        status.update(label=f"{name} 已建立索引", state="complete", expanded=False)
    return did, ""


def doc_label(d: dict) -> str:
    """按钮显示名：“精达股份 2025”。"""
    who = f"{d.get('company', '')} {d.get('year', '')}".strip()
    return who or d["name"]


def visible_ids():
    """公开模式下，每个会话只能看到示例年报和自己上传的；本地模式看到全部。"""
    return st.session_state.setdefault("my_docs", set()) if config.PUBLIC_DEMO else None


def current_theme() -> str:
    """组件的深浅色：优先用组件内手动切换的结果，否则跟随 Streamlit 主题。"""
    override = st.session_state.get("chat", {}).get("theme")
    if override in ("dark", "light"):
        return override
    try:
        return st.context.theme.type or "light"
    except Exception:
        return "light"


# ---------- 侧边栏：① 上传与文档选择 ----------
# 选择状态只有一个来源：st.session_state["doc_id"]。
# 上传只在“新文件”时把选择切过去一次，之后用户可以自由切换到任意已上传的年报。
with st.sidebar:
    up = st.file_uploader("上传年报 PDF", type=["pdf"],
                          disabled=config.PUBLIC_DEMO
                          and len(st.session_state.get("my_docs", ())) >= config.MAX_UPLOADS_PER_SESSION)
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

    # 旧版本索引自动用保存的原 PDF 重建，无需重新上传
    for d in list_documents(visible_ids()):
        if d.get("index_version", 1) != config.INDEX_VERSION:
            ingest((doc_dir(d["doc_id"]) / "source.pdf").read_bytes(), d["name"],
                   demo=d.get("demo", False))

    docs = {d["doc_id"]: d for d in list_documents(visible_ids())}
    if docs:
        if st.session_state.get("doc_id") not in docs:
            st.session_state.doc_id = next(iter(docs))
        st.markdown("**年报文档**")
        for did, d in docs.items():
            if st.button(doc_label(d), key=f"doc-{did}", use_container_width=True,
                         type="primary" if did == st.session_state.doc_id else "secondary"):
                st.session_state.doc_id = did
                st.session_state.pop("page", None)
                st.rerun()

    # 大模型 Key：公开模式下访客可以填自己的，不计入免费额度
    user_key = ""
    if config.PUBLIC_DEMO:
        st.divider()
        user_key = st.text_input("使用自己的 DeepSeek Key（可选）", type="password", key="user_key",
                                 help="填写后不再占用免费额度。Key 只保存在当前会话内存里，不会写入磁盘。"
                                 ).strip()
    elif not config.DEEPSEEK_API_KEY:
        st.warning("未检测到 DEEPSEEK_API_KEY，请在 .env 中配置。")
    st.caption("每次提问只检索上方选中的这一份年报；切换年报不会丢失各自的对话。")
    st.caption("若侧边栏显示异常，请关闭浏览器的“翻译此页”后刷新。")

if not docs:
    st.markdown("### 请先在左侧上传一份上市公司年报 PDF（文字版）")
    st.stop()

doc_id = st.session_state.doc_id
meta, retriever = get_retriever(doc_id, config.INDEX_VERSION)
meta = dict(meta)
meta["doc_id"] = doc_id
history = st.session_state.setdefault("histories", {}).setdefault(doc_id, [])

# 进行中的回合。切换年报就丢弃，避免状态串到另一份文档上。
turn = st.session_state.get("turn")
if turn and turn.get("doc_id") != doc_id:
    turn = None
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
messages = payload.build_messages(history, turn, page=page)
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
    ),
    key="chat",
)


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


def advance_turn() -> None:
    """回合状态机：retrieving -> generating -> done。

    关键是「先挂载组件、再做耗时工作」：组件挂载的增量会在几十毫秒内送达浏览器并绘制，
    之后的阻塞调用（检索、生成）期间用户看到的就是刚挂上去的那一帧。已实测成立。
    """
    target = st.session_state.get("turn")
    if not target or target.get("status") == "error":
        return

    if target["status"] == "retrieving":
        client = pick_client()
        if client is None:
            fail(target, "未配置 DeepSeek Key，无法回答。请在 .env 或 Secrets 中配置，"
                         "或在左侧填写自己的 Key。")
            st.rerun()
        try:
            hits, queries = retriever.search_with_queries(target["q"], client=client,
                                                          use_llm=config.LLM_REWRITE)
        except Exception as e:
            refund(target)
            fail(target, f"检索失败：{e}")
            st.rerun()
        target["hits"] = hits
        target["queries"] = queries
        target["status"] = "generating"
        st.session_state["turn"] = target
        st.rerun()

    if target["status"] == "generating":
        try:
            answer = "".join(generate(target["q"], target["hits"], client=pick_client(),
                                      company=meta.get("company", ""), year=meta.get("year", "")))
        except Exception as e:
            refund(target)
            fail(target, f"调用大模型失败：{e}")
            st.rerun()
        history.append({"q": target["q"], "a": answer, "hits": target["hits"],
                        "queries": target["queries"]})
        st.session_state.pop("turn", None)
        st.session_state["chat_warn"] = ""
        st.rerun()


# ---------- 来自组件的事件 ----------
if result.ask and not busy:
    asked = result.ask.get("text", "") if isinstance(result.ask, dict) else ""
    text = asked.strip()
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
            "doc_id": doc_id, "q": text, "status": "retrieving", "counted": count_it,
        }
        st.rerun()

if result.open_page and isinstance(result.open_page, dict):
    n = int(result.open_page.get("n") or 1)
    hits = history[-1].get("hits") if history else None
    if hits and 1 <= n <= len(hits):
        try:
            st.session_state["page"] = {
                "n": n, "png": page_data_uri(doc_id, int(hits[n - 1]["page_start"])),
            }
        except Exception:
            st.session_state["page"] = None
        st.rerun()

advance_turn()
