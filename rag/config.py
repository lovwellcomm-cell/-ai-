"""全局配置。优先级：环境变量 / .env > Streamlit secrets > 默认值。

本地开发把密钥写进 .env；部署到 Streamlit Community Cloud 时填在应用的 Secrets 里。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _secret(name: str):
    """读取 st.secrets 中的同名配置；没有 secrets 文件或没有该项时返回 None。"""
    try:
        import streamlit as st
        return st.secrets[name]
    except Exception:
        return None


def get(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is None:
        val = _secret(name)
    return str(val) if val is not None else default


# 国内网络可在 .env 里设 HF_ENDPOINT=https://hf-mirror.com；不设则使用官方地址。
# 必须在导入 huggingface_hub 前写入环境变量。
if get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = get("HF_ENDPOINT")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

DATA_DIR = ROOT / "data"
DEMO_DIR = ROOT / "demo"
DATA_DIR.mkdir(exist_ok=True)

DEEPSEEK_API_KEY = get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = get("LLM_MODEL", "deepseek-flash")

# bge-small-zh-v1.5 的 ONNX 导出版（与 BAAI 原版权重一致）
EMBED_MODEL = get("EMBED_MODEL", "Xenova/bge-small-zh-v1.5")
EMBED_ONNX_FILE = get("EMBED_ONNX_FILE", "onnx/model.onnx")
# bge 中文模型推荐的查询指令前缀
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

CHUNK_SIZE = int(get("CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(get("CHUNK_OVERLAP", "80"))
TOP_K = int(get("TOP_K", "8"))
# 任一年报章节最多占用几个检索位。年报语料极度倾斜（第八节财务报告常占三分之二），
# 不限制的话 TOP_K 个位置会被财报数字占满，模型拿不到“为什么”那部分材料。
MAX_PER_SECTION = int(get("MAX_PER_SECTION", "5"))
# 生成调用的超时（秒）。不设的话走 OpenAI 客户端默认的 600 秒，
# 一次卡住会把免费实例的会话钉死十分钟。
LLM_TIMEOUT = float(get("LLM_TIMEOUT", "60"))
# 给缺年份表头的报表片段前置表头。设为 0 可关掉（tools/eval_depth.py 做 A/B 时用）。
HEADER_PROPAGATION = get("HEADER_PROPAGATION", "1") == "1"
# 追问时带几轮历史给查询改写。只带问题，不带回答。
FOLLOWUP_HISTORY_TURNS = int(get("FOLLOWUP_HISTORY_TURNS", "2"))
# 查询改写的输出上限。这个模型是推理型的：实测一次改写要烧 1000+ 个推理 token，
# 而 max_tokens 把推理和正文一起算。原来设 200，正文一个字都剩不下、JSON 被截断、
# json.loads 抛异常后静默退化成规则改写 —— LLM 改写等于从来没生效过。
REWRITE_MAX_TOKENS = int(get("REWRITE_MAX_TOKENS", "3000"))
# 是否用大模型做查询改写（失败时自动退回规则改写）
LLM_REWRITE = get("LLM_REWRITE", "1") == "1"
# 索引格式版本：切分或前缀规则变化时 +1，旧索引会自动重建
INDEX_VERSION = 3

# ---------- 公开演示模式 ----------
# 部署到公网时设 PUBLIC_DEMO=1：限制提问次数、限制上传、按会话隔离上传的年报。
PUBLIC_DEMO = get("PUBLIC_DEMO", "0") == "1"
SESSION_LIMIT = int(get("SESSION_LIMIT", "5"))   # 每个会话免费提问次数
DAILY_LIMIT = int(get("DAILY_LIMIT", "100"))     # 全站每天免费提问次数
MAX_PAGES = int(get("MAX_PAGES", "400"))         # 上传年报的页数上限
MAX_UPLOADS_PER_SESSION = int(get("MAX_UPLOADS_PER_SESSION", "2"))
UPLOAD_TTL_HOURS = int(get("UPLOAD_TTL_HOURS", "24"))  # 访客上传文档的保留时长
