"""年报 AI 研究助手 —— Streamlit 界面（运行：streamlit run app.py）"""
import streamlit as st

from rag import config
from rag.chunker import chunk_pages
from rag.generator import cited_numbers, generate, get_client, make_client
from rag.indexer import (build_index, cleanup_uploads, doc_dir, doc_id_of, ensure_demo,
                         is_current, list_documents, load_index)
from rag.limits import Quota, check_and_consume
from rag.pdf_parser import extract_doc_info, parse_pdf, render_page_png
from rag.retriever import Retriever

st.set_page_config(page_title="年报 AI 研究助手", page_icon="📊", layout="wide")


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
    """下拉框显示名：“精达股份 2025 · 精达股份2025年报.pdf”。"""
    who = f"{d.get('company', '')} {d.get('year', '')}".strip()
    return f"{who} · {d['name']}" if who else d["name"]


def visible_ids():
    """公开模式下，每个会话只能看到示例年报和自己上传的；本地模式看到全部。"""
    return st.session_state.setdefault("my_docs", set()) if config.PUBLIC_DEMO else None


# ---------- 侧边栏：① 上传与文档选择 ----------
# 选择状态只有一个来源：下拉框（key="doc_id"）。上传只在“新文件”时把选择切过去一次，
# 之后上传框里残留的文件不再改写选择，用户可以自由切换到任意已上传的年报。
with st.sidebar:
    st.header("📁 年报文档")
    uploads_left = config.MAX_UPLOADS_PER_SESSION - len(st.session_state.get("my_docs", ()))
    can_upload = not config.PUBLIC_DEMO or uploads_left > 0
    up = st.file_uploader("上传年报 PDF", type=["pdf"], disabled=not can_upload)
    if up is not None and st.session_state.get("last_upload_id") != up.file_id:
        st.session_state.last_upload_id = up.file_id
        new_id, err = ingest(up.getvalue(), up.name, check_limits=config.PUBLIC_DEMO)
        st.session_state.upload_error = err
        if new_id:
            visible = visible_ids()
            if visible is not None:
                visible.add(new_id)
            st.session_state.doc_id = new_id  # 在创建下拉框之前设置，才会生效
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
        # 首次进入，或当前文档已不可见时，回落到第一份
        if st.session_state.get("doc_id") not in docs:
            st.session_state.doc_id = next(iter(docs))
        st.selectbox("当前检索的年报", list(docs), key="doc_id",
                     format_func=lambda i: doc_label(docs[i]))
        cur = docs[st.session_state.doc_id]
        st.caption(f"页数：{cur['pages']} ｜ 片段数：{cur['chunks']} ｜ 共 {len(docs)} 份")

    # 大模型 Key：公开模式下访客可以填自己的，不计入免费额度
    user_key = ""
    if config.PUBLIC_DEMO:
        st.divider()
        user_key = st.text_input("使用自己的 DeepSeek Key（可选）", type="password", key="user_key",
                                 help="填写后不再占用免费额度。Key 只保存在当前会话内存里，不会写入磁盘。"
                                 ).strip()
        if user_key:
            st.caption("已使用你自己的 Key，提问不限次数。")
        else:
            left = max(0, config.SESSION_LIMIT - st.session_state.get("used", 0))
            st.caption(f"本会话剩余免费提问：{left} / {config.SESSION_LIMIT} 次")
    elif not config.DEEPSEEK_API_KEY:
        st.warning("未检测到 DEEPSEEK_API_KEY，请在 .env 中配置。")
    st.caption("每次提问只检索上方选中的这一份年报；切换年报不会丢失各自的对话。")
    st.caption("若页面报 removeChild 错误，请关闭浏览器的“翻译此页”后刷新。")

st.title("📊 年报 AI 研究助手")
if not docs:
    st.info("请先在左侧上传一份上市公司年报 PDF（文字版）。")
    st.stop()

doc_id = st.session_state.doc_id
meta, retriever = get_retriever(doc_id, config.INDEX_VERSION)
st.caption(f"当前检索：**{doc_label(meta)}**（共 {meta['pages']} 页）")
# 每份年报各自保存对话，切换文档时互不覆盖
history = st.session_state.setdefault("histories", {}).setdefault(doc_id, [])


def show_sources(answer: str, hits: list[dict], turn: int):
    """⑥ 展示引用：被回答引用的片段在前，并可查看原页。"""
    cited = cited_numbers(answer, len(hits))
    st.markdown("**引用来源**")
    order = cited + [n for n in range(1, len(hits) + 1) if n not in cited]
    for n in order:
        h = hits[n - 1]
        if h["page_start"] == h["page_end"]:
            pages = f"第 {h['page_start']} 页"
        else:
            pages = f"第 {h['page_start']}–{h['page_end']} 页"
        mark = "✅" if n in cited else "▫️"
        where = h["section"] + (f" · {h['subsection']}" if h.get("subsection") else "")
        with st.expander(f"{mark} [{n}] {where} · {pages}"):
            st.text(h["text"])
            if st.toggle("查看原页", key=f"src-{doc_id}-{turn}-{h['chunk_id']}"):
                st.image(render_page(doc_id, h["page_start"]), caption=f"原文第 {h['page_start']} 页")


# 所有已完成的轮次都由这个循环渲染，元素树在每次重跑时保持一致，
# 避免流式输出和重跑之间结构突变导致前端 removeChild 报错。
for i, turn in enumerate(history):
    with st.chat_message("user"):
        st.write(turn["q"])
    with st.chat_message("assistant"):
        st.markdown(turn["a"])
        if turn.get("queries"):
            st.caption("检索词：" + " ｜ ".join(turn["queries"]))
        show_sources(turn["a"], turn["hits"], turn=i)


def pick_client():
    """本次提问使用的大模型客户端：访客自己的 Key 优先，否则用站长配置的 Key。"""
    if user_key:
        return make_client(user_key)
    return get_client() if config.DEEPSEEK_API_KEY else None


if q := st.chat_input(f"就《{doc_label(meta)}》提问，例如：营业收入和净利润分别是多少？"):
    client = pick_client()
    if client is None:
        st.error("未配置 DeepSeek Key，无法回答。请在 .env 或 Secrets 中配置，或在侧边栏填写自己的 Key。")
        st.stop()
    counted = False
    if config.PUBLIC_DEMO and not user_key:
        ok, why = check_and_consume(st.session_state.get("used", 0), config.SESSION_LIMIT, get_quota())
        if not ok:
            if why == "session":
                st.warning(f"本会话的 {config.SESSION_LIMIT} 次免费提问已用完。"
                           "可以在侧边栏填写自己的 DeepSeek Key 继续使用。")
            else:
                st.warning("今天的免费额度已被用完，明天再来，或在侧边栏填写自己的 DeepSeek Key。")
            st.stop()
        st.session_state.used = st.session_state.get("used", 0) + 1
        counted = True
    with st.chat_message("user"):
        st.write(q)
    with st.chat_message("assistant"):
        try:
            with st.spinner("检索相关片段…"):
                hits, queries = retriever.search_with_queries(q, client=client, use_llm=config.LLM_REWRITE)
            answer = st.write_stream(generate(q, hits, client=client, company=meta.get("company", ""),
                                              year=meta.get("year", "")))
        except Exception as e:  # 网络 / 密钥错误：退还额度
            if counted:
                get_quota().refund()
                st.session_state.used -= 1
            st.error(f"调用大模型失败：{e}")
            st.stop()
    history.append({"q": q, "a": answer, "hits": hits, "queries": queries})
    st.rerun()
