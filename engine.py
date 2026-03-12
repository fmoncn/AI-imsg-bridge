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
SUMMARY_PATTERNS = re.compile(
    r"总结|摘要|概况|进展|汇总|简报|总览|overview|summary|report|recap",
    re.IGNORECASE,
)
EXPLANATION_PATTERNS = re.compile(
    r"是什么|为什么|怎么|如何|介绍|说明|解释|分析|比较|建议|方案|思路|结论|原因|区别|recommend|explain|why|how|what is",
    re.IGNORECASE,
)
EXECUTION_PATTERNS = re.compile(
    r"修复|修改|改掉|重构|实现|编写|新增|删除|更新|运行|执行|测试|排查|检查|定位|提交|推送|部署|重启|查看日志|查日志|"
    r"fix|implement|write|update|refactor|run|execute|test|debug|investigate|check|commit|push|deploy|restart",
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
IDENTITY_PATTERNS = re.compile(
    r"^(你是谁|你现在的模型是什么|当前模型是什么|自我介绍一下|介绍一下自己)[!！。.\s]*$",
    re.IGNORECASE,
)
DETAIL_REQUEST_PATTERNS = re.compile(
    r"详细|展开|具体说说|细讲|逐步|完整|完整版|详细说明|详细分析|深入|depth|detailed|step by step",
    re.IGNORECASE,
)


def build_imessage_prompt(
    content: str,
    brief_mode: bool,
    max_lines: int,
    max_chars: int,
) -> str:
    if not brief_mode or DETAIL_REQUEST_PATTERNS.search(content):
        return content
    instruction = (
        "[iMessage回复要求]\n"
        "用简体中文，结果导向，先给结论再给必要信息。\n"
        f"默认不超过{max_lines}行、约{max_chars}字。\n"
        "非必要不要铺垫、客套、长解释；优先给结论、关键发现、下一步。\n\n"
    )
    return instruction + content


def is_execution_task(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if not (CODE_KEYWORDS.search(content) or EXECUTION_PATTERNS.search(content)):
        return False
    if SUMMARY_PATTERNS.search(content) or EXPLANATION_PATTERNS.search(content):
        return False
    if stripped.startswith("/"):
        return False
    return True


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


def build_command(
    model: str,
    full_content: str,
    cli_paths: dict[str, str],
    memory,
    codex_memory_turns: int,
    codex_reasoning_effort: str,
    gemini_model: str,
    brief_mode: bool,
    imessage_max_lines: int,
    imessage_max_chars: int,
) -> list[str]:
    path = cli_paths.get(model)
    prompt_content = build_imessage_prompt(full_content, brief_mode, imessage_max_lines, imessage_max_chars)
    if model == "claude":
        return [path, "-p", prompt_content, "--continue"] if memory.has_session("claude") else [path, "-p", prompt_content]
    if model == "gemini":
        ctx = memory.get_context("gemini", max_turns=3)
        prompt = f"{ctx}{prompt_content}" if ctx else prompt_content
        cmd = [path, "-y"]
        if gemini_model:
            cmd.extend(["-m", gemini_model])
        cmd.extend(["-p", prompt])
        return cmd
    if model == "codex":
        ctx = memory.get_context("codex", max_turns=codex_memory_turns)
        prompt = f"{ctx}{prompt_content}" if ctx else prompt_content
        return [path, "-c", f'model_reasoning_effort="{codex_reasoning_effort}"', "exec", prompt, "--skip-git-repo-check", "--full-auto"]
    return [path, prompt_content]


def canned_reply(content: str) -> str | None:
    stripped = content.strip()
    if ACK_ONLY_PATTERNS.match(stripped):
        return "收到。"
    if IDENTITY_PATTERNS.match(stripped):
        return None
    if SHORT_CHAT_PATTERNS.match(stripped):
        return "我在。直接说任务。"
    return None


def select_runtime_model(content: str, selected_model: str, has_attachment: bool, force_search: bool, disable_search: bool, auto_fast_routing: bool) -> str:
    stripped = content.strip()
    if not auto_fast_routing:
        return selected_model
    if selected_model == "claude":
        return "claude"
    if has_attachment:
        return "gemini"
    is_execution = is_execution_task(content)
    if force_search:
        return "gemini" if not is_execution else "codex"
    if disable_search:
        return "codex" if is_execution else "gemini"
    if IDENTITY_PATTERNS.match(stripped):
        return "gemini" if selected_model != "claude" else "claude"
    if SUMMARY_PATTERNS.search(content) and EXTERNAL_TOPIC_KEYWORDS.search(content):
        return "gemini"
    if SUMMARY_PATTERNS.search(content) and BRIDGE_KEYWORDS.search(content):
        return "gemini"
    if EXPLANATION_PATTERNS.search(content) and not is_execution:
        return "gemini"
    if not is_execution:
        return "gemini"
    if ACK_ONLY_PATTERNS.match(stripped) or SHORT_CHAT_PATTERNS.match(stripped):
        return "gemini"
    if (
        len(stripped) <= 16
        and not CODE_KEYWORDS.search(content)
        and not BRIDGE_KEYWORDS.search(content)
        and not LOCAL_ONLY_PATTERNS.search(content)
        and not EXTERNAL_TOPIC_KEYWORDS.search(content)
    ):
        return "gemini"
    return "codex"
