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
# 一次卡住会把免费实例的会话钉死十分钟。对**流式**调用这是「多久没收到数据」的上限，
# 不是总时长上限；长推理期间服务器不发数据，所以别设得太小。
LLM_TIMEOUT = float(get("LLM_TIMEOUT", "60"))
# 回答字数上限。实测这是最有效的提速手段：600 字比不限长度快 2.6 倍，
# 而且不是靠截断输出 —— 模型的推理量本身从 8297 个 token 降到 2845。
# 设 0 表示不限（会明显变慢，见 rag/generator.py:ANSWER_BUDGET_RULE 的实测数据）。
ANSWER_CHAR_BUDGET = int(get("ANSWER_CHAR_BUDGET", "600"))
# ---------- 扩展分析（第二次模型调用）----------
# 首轮回答之后再问一次模型「为什么会是这个结果」，作为独立一段追加在后面。
# 动机：实测发现模型一直在复述检索到的数字，没有做解释；而用户对「深度」的定义正是归因。
# 设 0 关闭，整条链路退回单次生成（测量 A/B 时用）。
ANALYZE_ENABLED = get("ANALYZE_ENABLED", "1") == "1"
# 扩展段的字数上限。理由同 ANSWER_CHAR_BUDGET：只有**明确的数字**才压得住推理 token，
# 「请简洁些」这类软约束实测无效。
ANALYZE_CHAR_BUDGET = int(get("ANALYZE_CHAR_BUDGET", "250"))
# 第二次调用的输出上限。单独给足 —— 这个模型是推理型的，max_tokens 把推理和正文一起算，
# 给少了正文一个字都剩不下（查询改写就是这么静默失效了几个月的）。
# 同时调用处会检查 finish_reason == "length"，截断必须显式报出来，不能当成功。
ANALYZE_MAX_TOKENS = int(get("ANALYZE_MAX_TOKENS", "3000"))
# 第二次调用的超时。比 LLM_TIMEOUT 短：首轮回答已经交付给用户了，
# 不该为了补充分析再把人挂住一分钟。
ANALYZE_TIMEOUT = float(get("ANALYZE_TIMEOUT", "45"))
# A/B 用：把「解释为什么」直接塞进首轮提示词，不调第二次。
# 如果它就能拿到大部分收益，那第二次调用就是多花一倍钱买一点点 ——
# 这是证伪这个功能最便宜的方式，所以留一个开关。
ANSWER_WHY_INLINE = get("ANSWER_WHY_INLINE", "0") == "1"
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

# ---------- 外部工具（Search / Fetch / 计算） ----------
# 只有首轮回答明确说「年报中未找到相关信息」时才启用：让模型调用工具补充公开信息，
# 而不是凭语言模型自身的记忆猜测。年报里能答的问题不会触发，保证引用仍然可核验。
WEB_SEARCH_ENABLED = get("WEB_SEARCH_ENABLED", "1") == "1"
# 一次搜索取几条。Search 接口按请求计费（0.005 元/次），不按条数。
WEB_SEARCH_MAX_RESULTS = int(get("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_TIMEOUT = float(get("WEB_SEARCH_TIMEOUT", "15"))
WEB_FETCH_TIMEOUT = float(get("WEB_FETCH_TIMEOUT", "20"))
# 工具循环最多几轮「模型要工具 → 我们执行 → 再问模型」。
WEB_TOOL_MAX_ROUNDS = int(get("WEB_TOOL_MAX_ROUNDS", "2"))
WEB_TOOL_MAX_TOKENS = int(get("WEB_TOOL_MAX_TOKENS", "2000"))
# 非流式调用的总时长上限。外部补充是追加段，不值得让用户无限等。
WEB_TOOL_TIMEOUT = float(get("WEB_TOOL_TIMEOUT", "60"))
# 补充段的字数上限。和扩展分析同理：只有明确的数字压得住推理 token。
WEB_CHAR_BUDGET = int(get("WEB_CHAR_BUDGET", "300"))
# 一轮最多登记多少个网页来源，防止模型反复搜索把引用编号撑爆。
WEB_MAX_SOURCES = int(get("WEB_MAX_SOURCES", "8"))

# ---------- 年报获取：粘贴巨潮链接 ----------
# 只接受巨潮资讯（cninfo）的 https PDF 地址；域名白名单写在 rag/disclosure.py。
REPORT_FETCH_ENABLED = get("REPORT_FETCH_ENABLED", "1") == "1"
REPORT_DOWNLOAD_MAX_MB = int(get("REPORT_DOWNLOAD_MAX_MB", "40"))
REPORT_DOWNLOAD_TIMEOUT = float(get("REPORT_DOWNLOAD_TIMEOUT", "30"))

# ---------- 公开演示模式 ----------
# 部署到公网时设 PUBLIC_DEMO=1：限制提问次数、限制上传、按会话隔离上传的年报。
PUBLIC_DEMO = get("PUBLIC_DEMO", "0") == "1"
SESSION_LIMIT = int(get("SESSION_LIMIT", "5"))   # 每个会话免费提问次数
DAILY_LIMIT = int(get("DAILY_LIMIT", "100"))     # 全站每天免费提问次数
MAX_PAGES = int(get("MAX_PAGES", "400"))         # 上传年报的页数上限
MAX_UPLOADS_PER_SESSION = int(get("MAX_UPLOADS_PER_SESSION", "2"))
UPLOAD_TTL_HOURS = int(get("UPLOAD_TTL_HOURS", "24"))  # 访客上传文档的保留时长
