"""④ 建立检索索引：向量（bge）+ 关键词（BM25），持久化到 data/<doc_id>/。"""
import builtins
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import config
from .chunker import Chunk

EmbedFn = Callable[[list[str]], np.ndarray]
_model = None
_MODEL_KEY = "arb.embed.default"


def doc_id_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def doc_dir(doc_id: str) -> Path:
    return config.DATA_DIR / doc_id


def _shared_models() -> dict:
    """进程级的模型缓存。放在 builtins 里，而不是模块变量。

    `rag` 包会被 `rag/freshness.py` 整体卸载重导（部署后进程没重启时），
    模块级变量随之归零；如果这时旧的检索器还活着，就会再建一份 ONNX 会话，
    免费实例上多出上百 MB，很容易 OOM。
    """
    return builtins.__dict__.setdefault("_arb_shared_models", {})


def _load_model():
    """从 hf-mirror 下载 bge-small-zh-v1.5 的 ONNX 版本，用 onnxruntime 在 CPU 上推理。

    不用 PyTorch：本机 Smart App Control 会拦截 torch 的原生 DLL。
    """
    cache = _shared_models()
    hit = cache.get(_MODEL_KEY)
    if hit is not None:
        return hit

    import onnxruntime as ort
    from huggingface_hub import snapshot_download
    from tokenizers import Tokenizer

    path = Path(snapshot_download(config.EMBED_MODEL, allow_patterns=[config.EMBED_ONNX_FILE, "tokenizer.json"]))
    tok = Tokenizer.from_file(str(path / "tokenizer.json"))
    tok.enable_truncation(max_length=512)
    tok.enable_padding(pad_id=0, pad_token="[PAD]")
    sess = ort.InferenceSession(str(path / config.EMBED_ONNX_FILE), providers=["CPUExecutionProvider"])
    cache[_MODEL_KEY] = (tok, sess)
    return tok, sess


def default_embed(texts: list[str], batch_size: int = 16) -> np.ndarray:
    """返回 L2 归一化的句向量（bge 用 [CLS] 位置作为句向量）。"""
    global _model
    if _model is None:
        _model = _load_model()
    tok, sess = _model
    input_names = {i.name for i in sess.get_inputs()}
    out = []
    for b in range(0, len(texts), batch_size):
        enc = tok.encode_batch(texts[b:b + batch_size])
        feeds = {
            "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64),
        }
        hidden = sess.run(None, {k: v for k, v in feeds.items() if k in input_names})[0]
        out.append(hidden[:, 0, :])
    vecs = np.concatenate(out).astype(np.float32)
    return vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)


def build_index(doc_id: str, pdf_bytes: bytes, name: str, n_pages: int,
                chunks: list[Chunk], embed: EmbedFn = default_embed,
                company: str = "", year: str = "", demo: bool = False,
                source_url: str = "", source_site: str = "") -> Path:
    d = doc_dir(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    src = d / "source.pdf"
    if not src.exists() or src.read_bytes() != pdf_bytes:
        src.write_bytes(pdf_bytes)
    texts = [c.index_text or c.text for c in chunks]
    emb = embed(texts) if chunks else np.zeros((0, 1), np.float32)
    np.save(d / "embeddings.npy", emb)
    (d / "chunks.json").write_text(
        json.dumps([c.to_dict() for c in chunks], ensure_ascii=False), encoding="utf-8")
    meta = {"doc_id": doc_id, "name": name, "pages": n_pages, "chunks": len(chunks),
            "company": company, "year": year, "index_version": config.INDEX_VERSION,
            **({"demo": True} if demo else {})}
    # 来源只在上传/导入时留痕；本地上传没有来源，就不写这两个键。
    if source_url:
        meta["source_url"] = source_url
        meta["source_site"] = source_site
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def is_indexed(doc_id: str) -> bool:
    d = doc_dir(doc_id)
    return all((d / f).exists() for f in ("embeddings.npy", "chunks.json", "meta.json"))


def is_current(doc_id: str) -> bool:
    """索引存在且版本与当前代码一致。"""
    if not is_indexed(doc_id):
        return False
    meta = json.loads((doc_dir(doc_id) / "meta.json").read_text(encoding="utf-8"))
    return meta.get("index_version", 1) == config.INDEX_VERSION


def list_documents(visible_ids=None) -> list[dict]:
    """列出已建索引的文档。visible_ids 不为 None 时，只返回示例文档和 visible_ids 里的文档。"""
    docs = []
    for d in sorted(config.DATA_DIR.iterdir()) if config.DATA_DIR.exists() else []:
        meta = d / "meta.json"
        if meta.exists():
            info = json.loads(meta.read_text(encoding="utf-8"))
            if visible_ids is None or info.get("demo") or info["doc_id"] in visible_ids:
                docs.append(info)
    return docs


def ensure_demo() -> list[str]:
    """把仓库里的示例年报（demo/）复制到 data/，已存在的跳过。返回新复制的 doc_id。"""
    copied = []
    if not config.DEMO_DIR.exists():
        return copied
    for src in sorted(config.DEMO_DIR.iterdir()):
        dst = config.DATA_DIR / src.name
        if (src / "meta.json").exists() and not (dst / "meta.json").exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            # 标记为示例文档，公开模式下所有访客可见、不会被清理
            meta = json.loads((dst / "meta.json").read_text(encoding="utf-8"))
            meta["demo"] = True
            (dst / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            copied.append(src.name)
    return copied


def cleanup_uploads(max_age_hours: float, now: float | None = None) -> list[str]:
    """删除超过 max_age_hours 小时的非示例文档，返回被删除的 doc_id。"""
    removed = []
    now = now if now is not None else time.time()
    for d in config.DATA_DIR.iterdir() if config.DATA_DIR.exists() else []:
        meta = d / "meta.json"
        if not meta.exists():
            continue
        if json.loads(meta.read_text(encoding="utf-8")).get("demo"):
            continue
        if now - meta.stat().st_mtime > max_age_hours * 3600:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


def load_index(doc_id: str):
    d = doc_dir(doc_id)
    chunks = json.loads((d / "chunks.json").read_text(encoding="utf-8"))
    emb = np.load(d / "embeddings.npy")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    return meta, chunks, emb
