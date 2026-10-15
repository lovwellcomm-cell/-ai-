from types import SimpleNamespace

from rag import config
from rag.chunker import chunk_pages
from rag.generator import build_messages, cited_numbers, generate
from rag.indexer import build_index, doc_id_of, is_indexed, load_index
from rag.pdf_parser import parse_pdf
from rag.retriever import Retriever
from tests.conftest import HEADER, fake_embed


def test_parse_pages_sections_and_noise(sample_pdf):
    pages = parse_pdf(sample_pdf)
    assert [p.page_no for p in pages] == [1, 2, 3, 4, 5, 6]
    assert pages[0].section.startswith("第一节")
    assert pages[1].section.startswith("第一节")
    assert all(p.section.startswith("第二节") for p in pages[2:])
    for p in pages:
        assert HEADER not in p.text          # 页眉被清洗
        assert str(p.page_no) not in p.text.splitlines()  # 页码行被清洗
    assert "128.5亿元" in pages[3].text


def test_chunks_do_not_cross_sections(sample_pdf):
    chunks = chunk_pages(parse_pdf(sample_pdf), size=40, overlap=10)
    assert len(chunks) >= 3
    for c in chunks:
        assert c.page_start <= c.page_end
        if c.section.startswith("第一节"):
            assert c.page_end <= 2
        else:
            assert c.page_start >= 3
    # 小块时要跨多个块
    assert any(c.page_start != c.page_end for c in chunk_pages(parse_pdf(sample_pdf), 500, 80))


def test_index_and_retrieve(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    pages = parse_pdf(sample_pdf)
    chunks = chunk_pages(pages, size=40, overlap=10)
    did = doc_id_of(sample_pdf)
    build_index(did, sample_pdf, "sample.pdf", len(pages), chunks, embed=fake_embed)
    assert is_indexed(did)
    meta, loaded, emb = load_index(did)
    assert meta["chunks"] == len(chunks) and emb.shape[0] == len(chunks)

    hits = Retriever(loaded, emb, embed=fake_embed).search("研发投入是多少", k=3)
    assert hits and "研发投入" in hits[0]["text"]
    assert hits[0]["page_start"] == 6


def test_generator_prompt_and_citations():
    chunks = [
        {"text": "营业收入为128.5亿元", "section": "第二节", "page_start": 4, "page_end": 4},
        {"text": "净利润为15.2亿元", "section": "第二节", "page_start": 5, "page_end": 5},
    ]
    msgs = build_messages("营收多少", chunks)
    assert "[1]（第二节，第4页）" in msgs[1]["content"]
    assert "[2]（第二节，第5页）" in msgs[1]["content"]

    def delta(s):
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=s))])

    captured = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return iter([delta("营收128.5亿元[1]"), delta("，净利润15.2亿元[2][9]")])

    answer = "".join(generate("营收多少", chunks, client=FakeClient()))
    assert captured["stream"] is True and captured["model"] == config.LLM_MODEL
    assert cited_numbers(answer, len(chunks)) == [1, 2]   # [9] 越界被忽略
    assert cited_numbers("见[1，2]与[2、1]", 2) == [1, 2]


def test_bookmarks_take_priority(bookmarked_pdf):
    pages = parse_pdf(bookmarked_pdf)
    assert [p.section for p in pages] == ["书签A", "书签B", "书签B"]


def test_render_page_png(sample_pdf):
    from rag.pdf_parser import render_page_png
    png = render_page_png(sample_pdf, 4)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- v2：子标题、上下文前缀、查询改写 ----------

def _statement_pdf() -> bytes:
    """封面带公司简称和年度；第2页合并利润表，第3页母公司利润表，两者都有“三、营业利润”。"""
    from fpdf import FPDF
    from tests.conftest import FONT, _need_font
    _need_font()
    pdf = FPDF()
    pdf.add_font("hei", fname=FONT)
    pdf.set_font("hei", size=11)
    pages = [
        ["公司代码：600000 公司简称：示例股份", "示例股份有限公司", "2025 年年度报告"],
        ["第八节 财务报告", "合并利润表", "一、营业总收入 1,000.00 900.00", "三、营业利润（亏损以“－”号填列） 300.00 250.00"],
        ["母公司利润表", "一、营业收入 100.00 90.00", "三、营业利润（亏损以“－”号填列） 30.00 25.00"],
        ["三、公司基本情况", "公司注册地为示例市。"],
    ]
    for lines in pages:
        pdf.add_page()
        y = 30
        for ln in lines:
            pdf.text(20, y, ln)
            y += 10
    return bytes(pdf.output())


def test_doc_info_and_statement_subsections():
    from rag.pdf_parser import extract_doc_info
    pages = parse_pdf(_statement_pdf())
    info = extract_doc_info(pages)
    assert (info.company, info.year) == ("示例股份", "2025")
    chunks = chunk_pages(pages, 500, 80, info)
    op = {c.subsection: c for c in chunks if "三、营业利润" in c.text}
    # 合并和母公司利润表分在不同片段，科目行“一、营业总收入”没有覆盖报表标题
    assert set(op) == {"合并利润表", "母公司利润表"}
    assert "300.00" in op["合并利润表"].text and "30.00" not in op["合并利润表"].text
    assert op["合并利润表"].index_text.startswith("示例股份2025年年度报告 | 第八节 财务报告 | 合并利润表")
    # 报表之后的附注小节退出报表上下文
    tail = [c for c in chunks if "注册地" in c.text][0]
    assert tail.subsection == "三、公司基本情况"


def test_rule_rewrite():
    from rag.retriever import rule_rewrite
    q = rule_rewrite("精达股份公司的25年营业利润是多少", "精达股份", "2025")
    assert "精达" not in q and "公司" not in q and "2025年营业利润" in q
    assert "2024年" in rule_rewrite("去年净利润", "精达股份", "2025")
    assert "上年同期" in rule_rewrite("营业收入有什么变化", "精达股份", "2025")


def test_llm_rewrite_fallback_and_merge():
    from rag.retriever import Retriever, llm_rewrite

    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise TimeoutError("timeout")

    assert llm_rewrite("营业利润", "2025", client=Boom()) == []

    class Ok:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    msg = SimpleNamespace(content='{"queries": ["合并利润表 营业利润", "主要会计数据 营业利润"]}')
                    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    chunks = [{"chunk_id": 0, "text": "三、营业利润 300.00", "index_text": "合并利润表\n三、营业利润 300.00",
               "section": "S", "subsection": "合并利润表", "page_start": 1, "page_end": 1}]
    emb = fake_embed([chunks[0]["index_text"]])
    r = Retriever(chunks, emb, embed=fake_embed, company="示例股份", year="2025",
                  use_llm_rewrite=True, llm_client=Ok())
    hits = r.search("示例股份25年营业利润")
    assert r.last_queries[0] == "2025年营业利润" and "合并利润表 营业利润" in r.last_queries
    assert hits[0]["chunk_id"] == 0
    # 改写失败时只用规则改写，检索照常
    r.llm_client = Boom()
    assert r.search("示例股份25年营业利润") and r.last_queries == ["2025年营业利润"]


def test_prompt_mentions_doc_and_subsection():
    chunks = [{"text": "三、营业利润 300.00 250.00", "section": "第八节", "subsection": "合并利润表",
               "page_start": 78, "page_end": 78}]
    sys_msg, user_msg = build_messages("营业利润", chunks, company="示例股份", year="2025")
    assert "示例股份 2025 年年度报告" in sys_msg["content"]
    assert "[1]（第八节 · 合并利润表，第78页）" in user_msg["content"]


# ---------- v4：公开演示模式 ----------

def test_quota_session_daily_and_rollover():
    from datetime import date, timedelta
    from rag.limits import Quota, check_and_consume
    day = {"d": date(2026, 10, 15)}
    q = Quota(daily_limit=3, today=lambda: day["d"])
    assert check_and_consume(0, 2, q) == (True, "")
    assert check_and_consume(2, 2, q) == (False, "session")      # 会话额度用完，不占全站额度
    assert q.used_today() == 1
    assert check_and_consume(0, 5, q)[0] and check_and_consume(0, 5, q)[0]
    assert check_and_consume(0, 5, q) == (False, "daily")        # 全站额度用完
    q.refund()
    assert check_and_consume(0, 5, q)[0]                         # 退还后可再用
    day["d"] += timedelta(days=1)
    assert q.used_today() == 0 and check_and_consume(0, 5, q)[0]  # 跨天清零


def test_quota_thread_safe():
    import threading
    from rag.limits import Quota
    q = Quota(daily_limit=50)
    ok = []
    threads = [threading.Thread(target=lambda: ok.append(q.try_consume())) for _ in range(200)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(ok) == 50 and q.used_today() == 50


def test_visible_docs_demo_and_cleanup(tmp_path, monkeypatch):
    import os
    import time
    from rag.indexer import cleanup_uploads, ensure_demo, list_documents
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DEMO_DIR", tmp_path / "demo")
    config.DATA_DIR.mkdir()
    for did in ("demo1", "up_a", "up_b"):
        d = (config.DEMO_DIR if did == "demo1" else config.DATA_DIR) / did
        d.mkdir(parents=True)
        (d / "meta.json").write_text(f'{{"doc_id": "{did}", "name": "{did}.pdf"}}', encoding="utf-8")
    assert ensure_demo() == ["demo1"] and ensure_demo() == []     # 只复制一次
    ids = lambda v: sorted(d["doc_id"] for d in list_documents(v))
    assert ids(None) == ["demo1", "up_a", "up_b"]                  # 本地模式：全部可见
    assert ids({"up_a"}) == ["demo1", "up_a"]                      # 公开模式：示例 + 自己的
    assert ids(set()) == ["demo1"]
    old = time.time() - 48 * 3600
    for did in ("demo1", "up_a"):
        os.utime(config.DATA_DIR / did / "meta.json", (old, old))
    assert cleanup_uploads(24) == ["up_a"]                         # 过期上传被清理，示例保留
    assert ids(None) == ["demo1", "up_b"]


def test_search_with_queries_is_independent_per_call():
    import threading
    from rag.retriever import Retriever
    chunks = [{"chunk_id": i, "text": f"营业利润 {i}", "index_text": f"营业利润 {i}", "section": "S",
               "subsection": "", "page_start": 1, "page_end": 1} for i in range(3)]
    r = Retriever(chunks, fake_embed([c["text"] for c in chunks]), embed=fake_embed,
                  company="示例股份", year="2025")
    out = {}

    def run(name, q):
        out[name] = r.search_with_queries(q)[1]

    ts = [threading.Thread(target=run, args=(n, q)) for n, q in
          (("a", "示例股份25年营业利润"), ("b", "示例股份去年营业利润"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out["a"] == ["2025年营业利润"] and out["b"] == ["2024年营业利润"]


def test_config_get_without_secrets(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_KEY", raising=False)
    assert config.get("SOME_UNSET_KEY", "dflt") == "dflt"        # 没有 secrets 文件也不报错
    monkeypatch.setenv("SOME_SET_KEY", "v")
    assert config.get("SOME_SET_KEY") == "v"
