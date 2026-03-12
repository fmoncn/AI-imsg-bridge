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
STATE_DIR = os.path.expanduser("~/.claude_bridge")
LOG_DIR = os.path.expanduser("~/.claude_bridge/logs")
PROCESS_REGISTRY_PATH = os.path.join(STATE_DIR, "active_processes.json")
HEALTH_STATE_PATH = os.path.join(STATE_DIR, "health_state.json")
STATE_DB_PATH = os.path.join(STATE_DIR, "bridge_state.sqlite")

# ── AI CLI 路径 ────────────────────────────────
CLI_PATHS = {
    "claude": os.getenv("CLAUDE_PATH", "/usr/local/bin/claude"),
    "gemini": os.getenv("GEMINI_PATH", "/usr/local/bin/gemini"),
    "codex":  os.getenv("CODEX_PATH",  "/usr/local/bin/codex"),
}

# ── 行为 ──────────────────────────────────────
DEFAULT_MODEL       = os.getenv("DEFAULT_MODEL", "claude")
CHUNK_SIZE          = int(os.getenv("CHUNK_SIZE", "2000"))
MEMORY_TURNS        = int(os.getenv("MEMORY_TURNS", "10"))   # 每个模型保留最近 N 轮对话
MEMORY_DIR          = os.path.expanduser("~/.claude_bridge/memory")
CODEX_MEMORY_TURNS  = int(os.getenv("CODEX_MEMORY_TURNS", "3"))
TIMEOUT_NORMAL      = int(os.getenv("TIMEOUT_NORMAL", "120"))
TIMEOUT_SEARCH      = int(os.getenv("TIMEOUT_SEARCH", "160"))
TIMEOUT_CODE        = int(os.getenv("TIMEOUT_CODE", "300"))
TIMEOUT_IMAGE       = int(os.getenv("TIMEOUT_IMAGE", "240"))
TASK_TIMEOUT        = TIMEOUT_CODE
MAX_MSG_LEN         = int(os.getenv("MAX_MSG_LEN", "8000"))
MAX_QUEUE_SIZE      = int(os.getenv("MAX_QUEUE_SIZE", "20"))
AUTO_ROUTE_IMAGES   = os.getenv("AUTO_ROUTE_IMAGES", "1") == "1"
HEARTBEAT_ENABLED   = os.getenv("HEARTBEAT_ENABLED", "1") == "1"
QUIET_HOURS_START   = int(os.getenv("QUIET_HOURS_START", "0"))
QUIET_HOURS_END     = int(os.getenv("QUIET_HOURS_END", "8"))
DANGEROUS_CONFIRMATION = os.getenv("DANGEROUS_CONFIRMATION", "1") == "1"
AUTO_FAST_ROUTING   = os.getenv("AUTO_FAST_ROUTING", "1") == "1"
CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "low")

# ── 联网搜索 ───────────────────────────────────
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT    = int(os.getenv("TAVILY_TIMEOUT", "25"))
PROGRESS_INTERVAL = int(os.getenv("PROGRESS_INTERVAL", "40"))  # 进度通知间隔（秒）

# ── PATH 补全（launchd 环境变量不完整时） ───────
ROBUST_PATH = (
    "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    f":{os.path.dirname(CLI_PATHS['claude'])}"
)
