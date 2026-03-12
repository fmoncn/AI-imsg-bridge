import os
import re
import subprocess
import tempfile


SEARCH_KEYWORDS = re.compile(
    r"最新|今天|现在|新闻|价格|股价|天气|哪里|什么时候|最近|昨天|明天|今年|本周|上周|实时|"
    r"现价|汇率|涨跌|发布|上市|热点|趋势|排行|latest|today|now|news|price|weather|"
    r"current|recent|trending|stock|rate|release",
    re.IGNORECASE,
)
EXTERNAL_TOPIC_KEYWORDS = re.compile(
    r"新闻|价格|股价|天气|汇率|比赛|航班|电影|票房|发布|上市|热点|趋势|排行|"
    r"官网|公司|产品|政策|行情|美元|人民币|黄金|比特币|BTC|AAPL|TSLA|"
    r"news|price|weather|stock|exchange|release|flight|traffic",
    re.IGNORECASE,
)
LOCAL_ONLY_PATTERNS = re.compile(
    r"你现在的模型|当前模型|本机|本地|这个项目|队列|状态|日志|bridge|main\.py|config\.py|"
    r"/status|/health|/memory|/queue|/tasks|你的提示词|你是谁",
    re.IGNORECASE,
)
CODE_KEYWORDS = re.compile(
    r"代码|程序|函数|脚本|写|开发|实现|调试|debug|fix|bug|code|script|function|class|api",
    re.IGNORECASE,
)
BRIDGE_KEYWORDS = re.compile(
    r"bridge|main\.py|config\.py|imessage|桥接|修复|升级|新功能|改一下|launchctl|功能|改进|优化",
    re.IGNORECASE,
)
ACK_ONLY_PATTERNS = re.compile(
    r"^(好|好的|好滴|收到|明白|嗯|嗯嗯|ok|okay|okk|yes|收到啦|知道了|继续|行|可以|收到谢谢|谢了|thanks|thank you)[!！。.\s]*$",
    re.IGNORECASE,
)
SHORT_CHAT_PATTERNS = re.compile(
    r"^(你好|hi|hello|在吗|在不在|早上好|下午好|晚上好|你是谁|介绍一下自己)[!！。.\s]*$",
    re.IGNORECASE,
)


def should_search(content: str, tavily_enabled: bool, force_search: bool = False, disable_search: bool = False) -> bool:
    if disable_search or not tavily_enabled:
        return False
    if force_search:
        return True
    if content.startswith("/"):
        return False
    if LOCAL_ONLY_PATTERNS.search(content):
        return False
    if not SEARCH_KEYWORDS.search(content):
        return False
    return bool(EXTERNAL_TOPIC_KEYWORDS.search(content))


def get_task_timeout(content: str, has_search: bool, has_attachment: bool, timeout_normal: int, timeout_search: int, timeout_code: int, timeout_image: int) -> int:
    if CODE_KEYWORDS.search(content):
        return timeout_code
    if has_attachment:
        return timeout_image
    if has_search:
        return timeout_search
    return timeout_normal


def prepare_image(raw_path: str, logger) -> str | None:
    path = os.path.expanduser(raw_path)
    if not os.path.exists(path):
        logger.warning(f"附件不存在: {path}")
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".heic", ".heif"):
        return path
    try:
        fd, jpg_path = tempfile.mkstemp(suffix=".jpg", prefix="bridge_img_")
        os.close(fd)
        subprocess.run(["sips", "-s", "format", "jpeg", path, "--out", jpg_path], check=True, capture_output=True)
        logger.info(f"🖼️ HEIC→JPEG: {jpg_path}")
        return jpg_path
    except Exception as exc:
        logger.warning(f"图片转换失败: {exc}")
        return None


def build_command(model: str, full_content: str, cli_paths: dict[str, str], memory, codex_memory_turns: int, codex_reasoning_effort: str) -> list[str]:
    path = cli_paths.get(model)
    if model == "claude":
        return [path, "-p", full_content, "--continue"] if memory.has_session("claude") else [path, "-p", full_content]
    if model == "gemini":
        ctx = memory.get_context("gemini", max_turns=3)
        prompt = f"{ctx}{full_content}" if ctx else full_content
        return [path, "-y", "-p", prompt]
    if model == "codex":
        ctx = memory.get_context("codex", max_turns=codex_memory_turns)
        prompt = f"{ctx}{full_content}" if ctx else full_content
        return [path, "-c", f'model_reasoning_effort="{codex_reasoning_effort}"', "exec", prompt, "--skip-git-repo-check", "--full-auto"]
    return [path, full_content]


def canned_reply(content: str) -> str | None:
    stripped = content.strip()
    if ACK_ONLY_PATTERNS.match(stripped):
        return "收到。"
    if SHORT_CHAT_PATTERNS.match(stripped):
        return "我在。直接说任务。"
    return None


def select_runtime_model(content: str, selected_model: str, has_attachment: bool, force_search: bool, disable_search: bool, auto_fast_routing: bool) -> str:
    if not auto_fast_routing:
        return selected_model
    if has_attachment:
        return "gemini"
    if selected_model != "codex":
        return selected_model
    if force_search:
        return "claude"
    if disable_search:
        return selected_model
    if ACK_ONLY_PATTERNS.match(content.strip()) or SHORT_CHAT_PATTERNS.match(content.strip()):
        return "claude"
    if (
        len(content.strip()) <= 16
        and not CODE_KEYWORDS.search(content)
        and not BRIDGE_KEYWORDS.search(content)
        and not LOCAL_ONLY_PATTERNS.search(content)
        and not EXTERNAL_TOPIC_KEYWORDS.search(content)
    ):
        return "claude"
    return selected_model
