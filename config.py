"""配置加载模块：从 .env 文件读取所有配置"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── 安全 ──────────────────────────────────────
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "")

# ── 账号 ──────────────────────────────────────
SENDER_IDS = [s.strip() for s in os.getenv("SENDER_IDS", "").split(",") if s.strip()]
SENDER_ID   = os.getenv("SENDER_ID", SENDER_IDS[0] if SENDER_IDS else "")

# ── 数据库 ────────────────────────────────────
DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
LOG_DIR = os.path.expanduser("~/.claude_bridge/logs")

# ── AI CLI 路径 ────────────────────────────────
CLI_PATHS = {
    "claude": os.getenv("CLAUDE_PATH", "/usr/local/bin/claude"),
    "gemini": os.getenv("GEMINI_PATH", "/usr/local/bin/gemini"),
    "codex":  os.getenv("CODEX_PATH",  "/usr/local/bin/codex"),
}

# ── 行为 ──────────────────────────────────────
DEFAULT_MODEL  = os.getenv("DEFAULT_MODEL", "claude")
TASK_TIMEOUT   = int(os.getenv("TASK_TIMEOUT", "300"))
CHUNK_SIZE     = int(os.getenv("CHUNK_SIZE", "2000"))
MEMORY_TURNS   = int(os.getenv("MEMORY_TURNS", "10"))   # 每个模型保留最近 N 轮对话
MEMORY_DIR     = os.path.expanduser("~/.claude_bridge/memory")

# ── PATH 补全（launchd 环境变量不完整时） ───────
ROBUST_PATH = (
    "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    f":{os.path.dirname(CLI_PATHS['claude'])}"
)
