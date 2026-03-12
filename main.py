import asyncio
import logging
import os
import re
import sys
import time
import traceback
import uuid

import aiohttp
from logging.handlers import RotatingFileHandler

from config import (
    AUTO_ROUTE_IMAGES,
    AUTO_FAST_ROUTING,
    BRIDGE_SECRET,
    CHUNK_SIZE,
    CLI_PATHS,
    DANGEROUS_CONFIRMATION,
    DB_PATH,
    DEFAULT_MODEL,
    HEALTH_STATE_PATH,
    HEARTBEAT_ENABLED,
    LOG_DIR,
    MAX_MSG_LEN,
    MAX_QUEUE_SIZE,
    MEMORY_DIR,
    MEMORY_TURNS,
    CODEX_MEMORY_TURNS,
    CODEX_REASONING_EFFORT,
    PROCESS_REGISTRY_PATH,
    PROGRESS_INTERVAL,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    ROBUST_PATH,
    SENDER_ID,
    SENDER_IDS,
    STATE_DIR,
    STATE_DB_PATH,
    TAVILY_API_KEY,
    TAVILY_SEARCH_URL,
    TAVILY_TIMEOUT,
    TASK_TIMEOUT,
    TIMEOUT_CODE,
    TIMEOUT_IMAGE,
    TIMEOUT_NORMAL,
    TIMEOUT_SEARCH,
)
from engine import (
    BRIDGE_KEYWORDS,
    CODE_KEYWORDS,
    EXTERNAL_TOPIC_KEYWORDS,
    LOCAL_ONLY_PATTERNS,
    canned_reply,
    build_command,
    get_task_timeout,
    prepare_image,
    select_runtime_model,
    should_search,
)
from message_store import IncomingMessage, fetch_new_messages, get_latest_marker
from process_utils import kill_registered_processes, register_process, terminate_process_tree, unregister_process
from router import command_arg, extract_search_directives, normalize_command
from state import AppState, ConversationMemory, ModelHealth, TaskRequest
from store import BridgeStore
from transport import send_chunked_message, send_imessage, strip_ansi


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SERVICE_LABEL = "com.fmon.claude_bridge"
LAUNCHD_TARGET = f"gui/{os.getuid()}/{SERVICE_LABEL}"

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_fh = RotatingFileHandler(os.path.join(LOG_DIR, "bridge.log"), maxBytes=5 * 1024 * 1024, backupCount=2)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger = logging.getLogger("ClaudeBridge")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
logger.addHandler(_fh)
logger.addHandler(_ch)

memory = ConversationMemory(MEMORY_DIR, MEMORY_TURNS, logger)
health = ModelHealth(HEALTH_STATE_PATH, logger)
app_state = AppState(DEFAULT_MODEL)
store = BridgeStore(STATE_DB_PATH, logger)
task_queue: asyncio.Queue[TaskRequest] = asyncio.Queue()

_QUOTA_PATTERN = re.compile(
    r"quota|rate.?limit|429|resource.?exhausted|too.?many.?request|limit.?exceed|"
    r"billing|payment|subscription|insufficient|capacity",
    re.IGNORECASE,
)
_DANGEROUS_KEYWORDS = re.compile(
    r"\brm\b|\bmv\b|\bchmod\b|\bchown\b|删除|格式化|清空|重置系统|卸载|kill\s|launchctl\s+(stop|unload)|"
    r"drop\s+table|truncate|shutdown|reboot",
    re.IGNORECASE,
)
_FALLBACK_CHAIN = {
    "gemini": ["claude", "codex"],
    "claude": ["gemini", "codex"],
    "codex": ["claude", "gemini"],
}
_BRIDGE_CONTEXT_PATH = os.path.expanduser("~/.claude_bridge/BRIDGE.md")
_USER_CONTEXT_PATH = os.path.expanduser("~/.claude_bridge/USER.md")


def verify_secret(content: str) -> tuple[bool, str]:
    if not BRIDGE_SECRET:
        return True, content
    if content.startswith(BRIDGE_SECRET + " "):
        return True, content[len(BRIDGE_SECRET) + 1:].strip()
    return False, content


async def tavily_search(query: str) -> str | None:
    try:
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TAVILY_SEARCH_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=TAVILY_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Tavily 返回 {resp.status}")
                    return None
                data = await resp.json()
        results = data.get("results", [])
        if not results:
            return None
        lines = ["[联网搜索结果]"]
        for index, result in enumerate(results[:3], 1):
            lines.append(f"{index}. {result.get('title', '')}")
            lines.append(f"   {result.get('content', '')[:120]}")
        lines.append("[以上为实时搜索结果，请结合回答]\n")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"Tavily 搜索失败: {exc}")
        return None

def rotate_stderr_log() -> None:
    stderr_log = os.path.join(LOG_DIR, "launch_stderr.log")
    max_size = 5 * 1024 * 1024
    keep_size = 1 * 1024 * 1024
    try:
        if os.path.exists(stderr_log) and os.path.getsize(stderr_log) > max_size:
            with open(stderr_log, "rb") as f:
                f.seek(-keep_size, 2)
                tail = f.read()
            with open(stderr_log, "wb") as f:
                f.write(b"[... truncated at startup, keeping last 1MB ...]\n")
                f.write(tail)
            logger.info("📋 launch_stderr.log 已截断（超过 5MB）")
    except Exception as exc:
        logger.warning(f"stderr 日志截断失败: {exc}")

def load_bridge_context(content: str) -> str:
    if not _BRIDGE_KEYWORDS.search(content):
        return ""
    parts = []
    for path, label in [(_USER_CONTEXT_PATH, "USER.md"), (_BRIDGE_CONTEXT_PATH, "BRIDGE.md")]:
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    parts.append(f.read())
        except Exception as exc:
            logger.warning(f"加载 {label} 失败: {exc}")
    if not parts:
        return ""
    logger.info("📎 注入项目上下文（USER + BRIDGE）")
    return f"[项目上下文]\n{'---'.join(parts)}\n[以上为背景，请基于此作答]\n\n"

async def progress_notifier(task: TaskRequest, stop_event: asyncio.Event) -> None:
    if PROGRESS_INTERVAL <= 0:
        return
    stage_messages = [
        "⏳ 任务仍在执行，网络或模型响应较慢",
        "⏳ 仍在处理中，已超过常见响应时间",
        "⏳ 任务较慢，继续等待中",
    ]
    await asyncio.sleep(PROGRESS_INTERVAL)
    stage = 0
    while not stop_event.is_set():
        elapsed = int(time.time() - app_state.task_start_time)
        prefix = stage_messages[min(stage, len(stage_messages) - 1)]
        await send_imessage(f"{prefix} ({elapsed}s)", task.recipient, logger)
        stage += 1
        await asyncio.sleep(PROGRESS_INTERVAL)


async def enqueue_task(task: TaskRequest) -> None:
    if task_queue.qsize() >= MAX_QUEUE_SIZE:
        await send_imessage(f"⚠️ 队列已满（{MAX_QUEUE_SIZE} 条），请稍后重试", task.recipient, logger)
        return
    if not task.task_id:
        task.task_id = store.create_task(task, status="queued")
    await task_queue.put(task)
    queue_size = task_queue.qsize()
    logger.info(f"📋 任务入队 task_id={task.task_id} rowid={task.rowid} model={task.model} queue={queue_size}")
    if queue_size > 1:
        await send_imessage(f"📋 已加入队列（前方还有 {queue_size - 1} 条）", task.recipient, logger)


def drain_queue() -> list[TaskRequest]:
    drained = []
    while not task_queue.empty():
        try:
            drained.append(task_queue.get_nowait())
            store.update_task_status(drained[-1].task_id, "cancelled", error="queue cleared")
            task_queue.task_done()
        except asyncio.QueueEmpty:
            break
    return drained


async def rebuild_queue(excluding_task_id: int | None = None) -> tuple[list[TaskRequest], list[TaskRequest]]:
    kept: list[TaskRequest] = []
    removed: list[TaskRequest] = []
    while not task_queue.empty():
        try:
            task = task_queue.get_nowait()
            task_queue.task_done()
            if excluding_task_id and task.task_id == excluding_task_id:
                removed.append(task)
            else:
                kept.append(task)
        except asyncio.QueueEmpty:
            break
    for task in kept:
        await task_queue.put(task)
    return kept, removed


def is_dangerous_request(content: str) -> bool:
    return DANGEROUS_CONFIRMATION and bool(_DANGEROUS_KEYWORDS.search(content))


def current_task_status() -> str:
    if not app_state.is_running or not app_state.current_task:
        latest = store.latest_task(statuses=["running"])
        if latest:
            return f"⏳ 执行中 | {latest['model'].upper()} | 任务 #{latest['id']}"
        return "💤 空闲"
    elapsed = int(time.time() - app_state.task_start_time)
    timeout = app_state.current_timeout or TASK_TIMEOUT
    task_id = f" #{app_state.current_task.task_id}" if app_state.current_task and app_state.current_task.task_id else ""
    return f"⏳ 执行中{task_id} {elapsed}s / {timeout}s | {app_state.current_task.model.upper()}"


def task_history_lines(limit: int = 3) -> list[str]:
    lines = ["🗂️ 最近任务："]
    for row in store.recent_tasks(limit=limit):
        snippet = row["content"][:24].replace("\n", " ")
        lines.append(f"#{row['id']} {row['model'].upper()} {row['status']} | {snippet}")
    return lines


def queue_snapshot_lines(limit: int = 5) -> list[str]:
    rows = store.tasks_by_status(["running", "queued", "waiting_confirm"], limit=limit)
    if not rows:
        return ["📋 当前无活动任务"]
    lines = ["📋 活动任务："]
    for row in rows:
        snippet = row["content"][:32].replace("\n", " ")
        lines.append(f"#{row['id']} {row['model'].upper()} {row['status']} | {snippet}")
    return lines


def format_task_detail(row) -> str:
    if not row:
        return "ℹ️ 未找到该任务"
    lines = [
        f"🧾 任务 #{row['id']}",
        f"模型: {row['model'].upper()} | 类型: {row['task_kind']} | 状态: {row['status']}",
        f"内容: {row['content'][:180]}",
    ]
    if row["output_excerpt"]:
        lines.append(f"结果摘要: {row['output_excerpt'][:300]}")
    if row["error"]:
        lines.append(f"错误: {row['error'][:200]}")
    return "\n".join(lines)


def task_request_from_row(row) -> TaskRequest:
    return TaskRequest(
        model=row["model"],
        content=row["content"],
        recipient=row["recipient"],
        attachment=row["attachment"],
        restore_model=row["restore_model"],
        force_search=bool(row["force_search"]),
        disable_search=bool(row["disable_search"]),
        rowid=row["message_rowid"],
        task_kind=row["task_kind"] or "task",
        review_group_id=row["review_group_id"],
        review_target_task_id=row["review_target_task_id"],
        review_role=row["review_role"],
    )


async def enqueue_review_tasks(recipient: str, target_task_id: int | None = None) -> bool:
    latest = store.get_task(target_task_id) if target_task_id else store.latest_completed_task()
    if not latest:
        await send_imessage("ℹ️ 没有可复审的已完成任务", recipient, logger)
        return True
    if latest["status"] != "done":
        await send_imessage(f"ℹ️ 任务 #{latest['id']} 尚未完成，暂时不能复审", recipient, logger)
        return True
    source_output = (latest["output_excerpt"] or "").strip()
    if not source_output:
        await send_imessage("ℹ️ 最近任务没有可用结果摘要，暂时无法复审", recipient, logger)
        return True
    review_prompt = (
        "请审查下面这项任务的结果，只输出关键问题与改进建议，优先关注正确性、遗漏、风险与更优方案。\n"
        f"[原任务 #{latest['id']}]\n{latest['content']}\n\n"
        f"[当前结果摘要]\n{source_output}\n"
    )
    group_id = uuid.uuid4().hex[:12]
    store.create_review_group(group_id, int(latest["id"]), recipient, total_reviews=2)
    tasks = [
        TaskRequest(
            model="claude",
            content=review_prompt,
            recipient=recipient,
            disable_search=True,
            task_kind="review",
            review_group_id=group_id,
            review_target_task_id=int(latest["id"]),
            review_role="claude",
        ),
        TaskRequest(
            model="gemini",
            content=review_prompt,
            recipient=recipient,
            disable_search=True,
            task_kind="review",
            review_group_id=group_id,
            review_target_task_id=int(latest["id"]),
            review_role="gemini",
        ),
    ]
    for task in tasks:
        task.task_id = store.create_task(task, status="queued")
        await task_queue.put(task)
    await send_imessage(f"🔍 已发起双模型复审，目标任务 #{latest['id']}", recipient, logger)
    return True


async def maybe_send_review_summary(task: TaskRequest) -> None:
    if not task.review_group_id:
        return
    group = store.review_group(task.review_group_id)
    if not group or int(group["summary_sent"]) == 1:
        return
    review_rows = store.review_tasks(task.review_group_id)
    if len(review_rows) < int(group["total_reviews"]):
        return
    if any(row["status"] not in ("done", "failed", "timeout", "cancelled") for row in review_rows):
        return

    target_id = int(group["target_task_id"])
    role_map = {row["review_role"] or row["model"]: row for row in review_rows}
    claude_row = role_map.get("claude")
    gemini_row = role_map.get("gemini")

    def review_text(row, label: str) -> str:
        if not row:
            return f"{label}: 无结果"
        if row["status"] != "done":
            return f"{label}: {row['status']} ({row['error'] or '无详情'})"
        return (row["output_excerpt"] or "无摘要").strip()[:500]

    lines = [
        f"【REVIEW】任务 #{target_id}",
        "Claude 视角：",
        review_text(claude_row, "Claude"),
        "",
        "Gemini 视角：",
        review_text(gemini_row, "Gemini"),
        "",
        "汇总结论：",
        "优先处理两边都指出的问题；如有冲突，以更具体、可复现、风险更高的一方为准。",
    ]
    store.mark_review_group_sent(task.review_group_id)
    await send_imessage("\n\n".join(lines), task.recipient, logger)


async def heartbeat() -> None:
    if not HEARTBEAT_ENABLED:
        logger.info("💓 心跳已禁用")
        return
    await asyncio.sleep(30 * 60)
    while True:
        hour = time.localtime().tm_hour
        if QUIET_HOURS_START <= hour < QUIET_HOURS_END:
            await asyncio.sleep(5 * 60)
            continue
        try:
            ts = time.strftime("%H:%M")
            await send_imessage(f"💓 Bridge 在线 | {ts}", SENDER_ID, logger)
            logger.info(f"💓 心跳自检通过 ({ts})")
        except Exception as exc:
            logger.error(f"💓 心跳自检异常: {exc}")
        await asyncio.sleep(30 * 60)


async def schedule_service_restart(recipient: str) -> bool:
    command = f"sleep 2; launchctl kickstart -k {LAUNCHD_TARGET}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/zsh",
            "-lc",
            command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(f"🔄 已调度服务重启 helper pid={proc.pid}")
        await send_imessage("🔄 Bridge 正在重启服务，约 2-5 秒恢复", recipient, logger)
        return True
    except Exception as exc:
        logger.error(f"调度服务重启失败: {exc}")
        await send_imessage(f"⚠️ 重启服务失败: {exc}", recipient, logger)
        return False


async def launchd_service_status() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "launchctl",
            "list",
            SERVICE_LABEL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            return f"🔧 launchd: 未找到服务或查询失败\n{err or SERVICE_LABEL}"
        text = stdout.decode("utf-8", errors="ignore").strip()
        return f"🔧 launchd 服务状态\n{text}"
    except Exception as exc:
        return f"⚠️ 查询 launchd 状态失败: {exc}"


async def handle_control_command(content: str, recipient: str) -> bool:
    cmd_lower = normalize_command(content)

    if cmd_lower == "/ping":
        await send_imessage("🏓 Pong!", recipient, logger)
        return True

    if cmd_lower == "/status":
        uptime_sec = int(time.time() - app_state.start_time)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = f"{hours}小时{minutes}分{seconds}秒" if hours else f"{minutes}分{seconds}秒"
        search_status = "✅ 已启用" if TAVILY_API_KEY else "❌ 未配置"
        counts = store.task_counts()
        active_count = counts.get("running", 0) + counts.get("queued", 0) + counts.get("waiting_confirm", 0)
        msg = (
            f"🤖 当前模型: {app_state.selected_model.upper()}\n"
            f"{current_task_status()}\n"
            f"⏱️ 运行时长: {uptime}\n"
            f"📋 活动任务: {active_count} 条（运行 {counts.get('running', 0)} / 排队 {counts.get('queued', 0)} / 待确认 {counts.get('waiting_confirm', 0)}）\n"
            f"💬 {app_state.selected_model}: {memory.summary(app_state.selected_model)}\n"
            f"🔍 联网搜索: {search_status}\n"
            f"🔒 口令: {'已启用' if BRIDGE_SECRET else '未启用'}\n"
            f"🧾 待确认: {app_state.pending_summary()}\n"
            + "\n".join(task_history_lines())
            + "\n"
            f"📊 模型健康:\n"
            + "\n".join(
                f"{'▶' if model == app_state.selected_model else ' '} {model.upper()}: {health.status_line(model)}"
                for model in ("claude", "gemini", "codex")
            )
        )
        await send_imessage(msg, recipient, logger)
        return True

    if cmd_lower == "/service status":
        await send_imessage(await launchd_service_status(), recipient, logger)
        return True

    if cmd_lower == "/health":
        lines = ["📊 模型健康详情："]
        for model in ("claude", "gemini", "codex"):
            mark = "▶" if model == app_state.selected_model else " "
            lines.append(f"{mark} {model.upper()}: {health.status_line(model)}")
        lines.append("\n🔄 互救链：")
        for model, chain in _FALLBACK_CHAIN.items():
            available = [fallback for fallback in chain if health.is_available(fallback)]
            lines.append(f"  {model.upper()} → {'/'.join(available).upper() if available else '无可用备援'}")
        await send_imessage("\n".join(lines), recipient, logger)
        return True

    if cmd_lower == "/memory":
        lines = ["📚 对话记忆状态："]
        for model in ("claude", "gemini", "codex"):
            mark = "▶" if model == app_state.selected_model else " "
            lines.append(f"{mark} {model.upper()}: {memory.summary(model)}")
        await send_imessage("\n".join(lines), recipient, logger)
        return True

    if cmd_lower == "/queue":
        await send_imessage("\n".join(queue_snapshot_lines()), recipient, logger)
        return True

    if cmd_lower == "/review":
        arg = command_arg(content)
        task_id = int(arg) if arg.isdigit() else None
        return await enqueue_review_tasks(recipient, task_id)

    if cmd_lower in ("/tasks", "/task list"):
        counts = store.task_counts()
        lines = [
            "🗂️ 任务面板：",
            f"运行 {counts.get('running', 0)} | 排队 {counts.get('queued', 0)} | 待确认 {counts.get('waiting_confirm', 0)} | 完成 {counts.get('done', 0)} | 失败 {counts.get('failed', 0)} | 超时 {counts.get('timeout', 0)}",
        ]
        lines.extend(queue_snapshot_lines(limit=8)[1:])
        lines.extend(task_history_lines(limit=5)[1:])
        await send_imessage("\n".join(lines), recipient, logger)
        return True

    if cmd_lower.startswith("/task "):
        arg = command_arg(content)
        if arg.startswith("cancel "):
            task_id_text = arg.split(maxsplit=1)[1].strip() if len(arg.split(maxsplit=1)) > 1 else ""
            if not task_id_text.isdigit():
                await send_imessage("⚠️ 用法: /task cancel 任务ID", recipient, logger)
                return True
            task_id = int(task_id_text)
            if app_state.current_task and app_state.current_task.task_id == task_id and app_state.current_process:
                terminate_process_tree(app_state.current_process, logger)
                store.update_task_status(task_id, "cancelled", error="cancelled by user")
                await send_imessage(f"🛑 已取消运行中任务 #{task_id}", recipient, logger)
                return True
            _, removed = await rebuild_queue(excluding_task_id=task_id)
            if removed:
                store.update_task_status(task_id, "cancelled", error="cancelled by user")
                await send_imessage(f"🧹 已取消排队任务 #{task_id}", recipient, logger)
            else:
                await send_imessage(f"ℹ️ 任务 #{task_id} 不在运行中或队列中", recipient, logger)
            return True
        if arg.startswith("retry "):
            task_id_text = arg.split(maxsplit=1)[1].strip() if len(arg.split(maxsplit=1)) > 1 else ""
            if not task_id_text.isdigit():
                await send_imessage("⚠️ 用法: /task retry 任务ID", recipient, logger)
                return True
            row = store.get_task(int(task_id_text))
            if not row:
                await send_imessage("ℹ️ 未找到该任务", recipient, logger)
                return True
            if row["status"] in ("running", "queued", "waiting_confirm"):
                await send_imessage(f"ℹ️ 任务 #{row['id']} 当前仍在活动中，无需重试", recipient, logger)
                return True
            new_task = task_request_from_row(row)
            new_task.task_id = store.create_task(new_task, status="queued", task_kind=row["task_kind"])
            await task_queue.put(new_task)
            await send_imessage(f"🔁 已重新入队任务 #{row['id']} -> 新任务 #{new_task.task_id}", recipient, logger)
            return True
        if not arg.isdigit():
            await send_imessage("⚠️ 用法: /task 任务ID", recipient, logger)
            return True
        row = store.get_task(int(arg))
        await send_imessage(format_task_detail(row), recipient, logger)
        return True

    if cmd_lower == "/stop":
        if app_state.current_process and app_state.is_running:
            terminate_process_tree(app_state.current_process, logger)
            await send_imessage("🛑 已中断当前任务", recipient, logger)
        else:
            await send_imessage("💤 当前没有运行中的任务", recipient, logger)
        return True

    if cmd_lower in ("/cancel all", "/clear queue"):
        stopped = False
        if app_state.current_process and app_state.is_running:
            stopped = terminate_process_tree(app_state.current_process, logger)
        drained = drain_queue()
        await send_imessage(f"🧹 已清空 {len(drained)} 条排队任务" + ("，并中断当前任务" if stopped else ""), recipient, logger)
        return True

    if cmd_lower == "/restart":
        await schedule_service_restart(recipient)
        return True

    if cmd_lower == "/reset":
        memory.reset(app_state.selected_model)
        await send_imessage(f"🗑️ 已清空 {app_state.selected_model.upper()} 对话历史", recipient, logger)
        return True

    if cmd_lower == "/reset all":
        memory.reset_all()
        await send_imessage("🗑️ 已清空所有模型对话历史", recipient, logger)
        return True

    if cmd_lower == "/confirm":
        if not app_state.pending_confirmation:
            restored = store.get_pending_confirmation(recipient)
            if restored:
                app_state.pending_confirmation = restored
        if not app_state.pending_confirmation:
            await send_imessage("ℹ️ 当前没有待确认的高风险任务", recipient, logger)
            return True
        task = app_state.pending_confirmation
        app_state.pending_confirmation = None
        store.clear_pending_confirmation(recipient)
        await enqueue_task(task)
        await send_imessage("✅ 已确认，高风险任务已入队", recipient, logger)
        return True

    if cmd_lower == "/help":
        await send_imessage(
            "📖 指令列表：\n"
            "/c  → Claude Code\n"
            "/g  → Gemini\n"
            "/x  → Codex\n"
            "/web 内容 → 强制联网搜索\n"
            "/local 内容 → 禁止联网搜索\n"
            "/status  → 状态 + 模型健康\n"
            "/service status → launchd 服务状态\n"
            "/health  → 健康详情 + 互救链\n"
            "/memory  → 记忆状态\n"
            "/queue   → 查看待处理队列\n"
            "/tasks   → 查看任务面板\n"
            "/task 123 → 查看任务详情\n"
            "/task cancel 123 → 取消任务\n"
            "/task retry 123 → 重试任务\n"
            "/review  → 复审最近完成任务\n"
            "/review 123 → 复审指定任务\n"
            "/stop    → 中断当前任务\n"
            "/cancel all → 清空队列并中断当前任务\n"
            "/clear queue → 清空排队任务\n"
            "/restart → 重启 Bridge 服务\n"
            "/reset   → 清空当前模型历史\n"
            "/reset all → 清空所有历史\n"
            "/confirm → 确认高风险任务\n"
            "/ping    → 心跳检测\n"
            "/help    → 本帮助",
            recipient,
            logger,
        )
        return True

    return False


async def run_ai_task(task: TaskRequest) -> None:
    converted_img = None
    stop_event = asyncio.Event()
    progress_task = None
    has_search = should_search(task.content, bool(TAVILY_API_KEY), task.force_search, task.disable_search)
    timeout = get_task_timeout(task.content, has_search, bool(task.attachment), TIMEOUT_NORMAL, TIMEOUT_SEARCH, TIMEOUT_CODE, TIMEOUT_IMAGE)

    logger.info(
        f"▶️ [rowid={task.rowid}] [{task.model}] search={has_search} attachment={bool(task.attachment)} "
        f"timeout={timeout}s queue={task_queue.qsize()}"
    )
    store.update_task_status(task.task_id, "running")
    memory.add(task.model, "user", task.content)

    try:
        path = CLI_PATHS.get(task.model)
        if not path or not os.path.exists(path):
            store.update_task_status(task.task_id, "failed", error=f"missing cli path: {path}")
            await send_imessage(f"❌ 未找到 {task.model} 路径: {path}", task.recipient, logger)
            return

        search_prefix = ""
        if has_search:
            await send_imessage("🔍 正在联网搜索...", task.recipient, logger)
            search_result = await tavily_search(task.content)
            if search_result:
                search_prefix = search_result
                logger.info("✅ 搜索结果已附加")

        img_note = ""
        if task.attachment:
            converted_img = prepare_image(task.attachment, logger)
            if converted_img:
                img_note = f"[用户发送了图片: {converted_img}]\n"
                logger.info(f"🖼️ 附件就绪: {os.path.basename(converted_img)}")

        bridge_ctx = load_bridge_context(task.content)
        full_content = f"{bridge_ctx}{search_prefix}{img_note}{task.content}"
        cmd_env = os.environ.copy()
        cmd_env["NO_COLOR"] = "1"
        cmd_env["TERM"] = "dumb"
        cmd_env["PATH"] = f"{ROBUST_PATH}:{cmd_env.get('PATH', '')}"

        cmd = build_command(task.model, full_content, CLI_PATHS, memory, CODEX_MEMORY_TURNS, CODEX_REASONING_EFFORT)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=cmd_env,
            cwd=PROJECT_ROOT,
            start_new_session=True,
        )
        register_process(PROCESS_REGISTRY_PATH, process.pid, logger)
        app_state.set_running(task, process, timeout)
        progress_task = asyncio.create_task(progress_notifier(task, stop_event))

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            terminate_process_tree(process, logger)
            health.record_failure(task.model, "timeout")
            store.update_task_status(task.task_id, "timeout", error=f">{timeout}s")
            await send_imessage(f"⚠️ {task.model} 任务超时（>{timeout}s）", task.recipient, logger)
            return

        if process.returncode in (-15, -9):
            store.update_task_status(task.task_id, "cancelled", error=f"signal {process.returncode}")
            return

        raw = stdout.decode("utf-8", errors="ignore") if stdout else stderr.decode("utf-8", errors="ignore") if stderr else ""
        output = strip_ansi(raw).strip()

        if not output:
            health.record_failure(task.model, "empty response")
            store.update_task_status(task.task_id, "failed", error="empty response")
            await send_imessage(f"⚠️ {task.model} 返回了空结果", task.recipient, logger)
            return

        if _QUOTA_PATTERN.search(output):
            health.record_failure(task.model, "quota exhausted", quota=True)
            fallback = next((model for model in _FALLBACK_CHAIN.get(task.model, []) if health.is_available(model)), None)
            if fallback:
                store.update_task_status(task.task_id, "failed", error="quota exhausted -> fallback")
                await send_imessage(
                    f"⚠️ {task.model.upper()} 配额耗尽\n🔄 自动切换 {fallback.upper()} 重试...",
                    task.recipient,
                    logger,
                )
                await run_ai_task(
                    TaskRequest(
                        model=fallback,
                        content=task.content,
                        recipient=task.recipient,
                        attachment=task.attachment,
                        force_search=task.force_search,
                        disable_search=task.disable_search,
                        rowid=task.rowid,
                    )
                )
            else:
                store.update_task_status(task.task_id, "failed", error="all quota exhausted")
                await send_imessage("⛔ 所有模型配额耗尽，请稍后重试", task.recipient, logger)
            return

        health.record_success(task.model)
        memory.add(task.model, "assistant", output[:500])
        store.update_task_result(task.task_id, output)
        store.update_task_status(task.task_id, "done")
        if task.task_kind == "review" and task.review_group_id:
            await maybe_send_review_summary(task)
        else:
            await send_chunked_message(output, task.recipient, task.model, CHUNK_SIZE, logger)
    except Exception as exc:
        health.record_failure(task.model, str(exc)[:80])
        store.update_task_status(task.task_id, "failed", error=str(exc)[:200])
        logger.error(f"执行异常: {exc}\n{traceback.format_exc()}")
        await send_imessage(f"⚠️ 脚本异常: {exc}", task.recipient, logger)
    finally:
        stop_event.set()
        if progress_task:
            progress_task.cancel()
        if app_state.current_process:
            unregister_process(PROCESS_REGISTRY_PATH, app_state.current_process.pid)
        app_state.clear_running()
        if task.restore_model and app_state.selected_model != task.restore_model:
            app_state.selected_model = task.restore_model
            await send_imessage(f"🔄 已恢复至 {task.restore_model.upper()}", task.recipient, logger)
        if converted_img and converted_img != task.attachment:
            try:
                os.remove(converted_img)
            except Exception:
                pass


async def queue_worker() -> None:
    while True:
        task = await task_queue.get()
        await run_ai_task(task)
        task_queue.task_done()


async def handle_incoming_message(message: IncomingMessage) -> None:
    raw_content = (message.text or "").strip()
    if not raw_content and message.attachment:
        raw_content = "请描述这张图片"
    if not raw_content and not message.attachment:
        return

    ok, content = verify_secret(raw_content)
    if not ok:
        await send_imessage("🔒 未授权", SENDER_ID, logger)
        return

    logger.info(
        f"📥 rowid={message.rowid} sender={message.sender} content={content[:80]!r}"
        + (f" [📎{os.path.basename(message.attachment)}]" if message.attachment else "")
    )

    if content.lower() == "/c":
        app_state.selected_model = "claude"
        store.set_selected_model(SENDER_ID, "claude")
        await send_imessage(f"✅ 已切换至 Claude Code\n💬 {memory.summary('claude')}", SENDER_ID, logger)
        return
    if content.lower() == "/g":
        app_state.selected_model = "gemini"
        store.set_selected_model(SENDER_ID, "gemini")
        await send_imessage(f"✅ 已切换至 Gemini\n💬 {memory.summary('gemini')}", SENDER_ID, logger)
        return
    if content.lower() == "/x":
        app_state.selected_model = "codex"
        store.set_selected_model(SENDER_ID, "codex")
        await send_imessage(f"✅ 已切换至 Codex\n💬 {memory.summary('codex')}", SENDER_ID, logger)
        return

    if content.startswith("/") and await handle_control_command(content, SENDER_ID):
        return

    content, force_search, disable_search = extract_search_directives(content)

    if not content:
        await send_imessage("⚠️ 指令后需要跟内容", SENDER_ID, logger)
        return

    fast_reply = canned_reply(content)
    if fast_reply:
        await send_imessage(fast_reply, SENDER_ID, logger)
        return

    if len(content) > MAX_MSG_LEN:
        await send_imessage(f"⚠️ 消息过长（{len(content)} 字），已截取前 {MAX_MSG_LEN} 字处理", SENDER_ID, logger)
        content = content[:MAX_MSG_LEN]

    target_model = select_runtime_model(
        content,
        app_state.selected_model,
        bool(message.attachment),
        force_search,
        disable_search,
        AUTO_FAST_ROUTING,
    )
    restore_model = None
    if message.attachment and AUTO_ROUTE_IMAGES and target_model != "gemini":
        restore_model = target_model
        target_model = "gemini"
        await send_imessage(f"🖼️ 图片已路由至 Gemini 处理，完成后恢复 {restore_model.upper()}", SENDER_ID, logger)
    elif target_model != app_state.selected_model:
        logger.info(f"⚡ 快速路由: {app_state.selected_model.upper()} -> {target_model.upper()} | {content[:40]!r}")

    task = TaskRequest(
        model=target_model,
        content=content,
        recipient=SENDER_ID,
        attachment=message.attachment,
        restore_model=restore_model,
        force_search=force_search,
        disable_search=disable_search,
        rowid=message.rowid,
    )

    if is_dangerous_request(content):
        task.task_id = store.create_task(task, status="waiting_confirm")
        app_state.pending_confirmation = task
        store.set_pending_confirmation(SENDER_ID, task)
        await send_imessage("⚠️ 检测到高风险任务，回复 /confirm 后执行", SENDER_ID, logger)
        return

    await enqueue_task(task)


async def main() -> None:
    logger.info(
        f"🚀 启动！默认模型: {app_state.selected_model} | "
        f"安全口令: {'已启用' if BRIDGE_SECRET else '⚠️ 未启用'} | "
        f"Tavily: {'✅' if TAVILY_API_KEY else '❌'}"
    )
    for model in ("claude", "gemini", "codex"):
        logger.info(f"   [{model}] {memory.summary(model)}")

    rotate_stderr_log()
    killed = kill_registered_processes(PROCESS_REGISTRY_PATH, logger)
    if killed:
        logger.info(f"🧹 已清理 {len(killed)} 个遗留进程组")

    if not SENDER_IDS:
        logger.error("❌ SENDER_IDS 未配置，请检查 .env 文件")
        return

    persisted_model = store.get_selected_model(SENDER_ID, DEFAULT_MODEL)
    app_state.selected_model = persisted_model

    persisted_offset = store.get_offset(SENDER_ID)
    if persisted_offset != (0, 0):
        app_state.set_last_seen(*persisted_offset)
    else:
        latest_marker = get_latest_marker(DB_PATH, SENDER_IDS, logger)
        app_state.set_last_seen(*latest_marker)
        store.set_offset(SENDER_ID, *latest_marker)

    pending = store.get_pending_confirmation(SENDER_ID)
    if pending:
        app_state.pending_confirmation = pending

    asyncio.create_task(queue_worker())
    asyncio.create_task(heartbeat())

    await send_imessage(
        f"🚀 Bridge 已启动 | 模型: {app_state.selected_model.upper()} | {time.strftime('%H:%M')}",
        SENDER_ID,
        logger,
    )

    while True:
        try:
            messages = fetch_new_messages(
                DB_PATH,
                SENDER_IDS,
                app_state.last_message_date,
                app_state.last_message_rowid,
                logger,
            )
            app_state.db_error_count = 0
            for message in messages:
                app_state.set_last_seen(message.date, message.rowid)
                store.set_offset(SENDER_ID, message.date, message.rowid)
                await handle_incoming_message(message)
        except Exception as exc:
            logger.error(f"主循环异常: {exc}")
            app_state.db_error_count += 1
            if app_state.db_error_count >= 5:
                await send_imessage("⚠️ Bridge 检测到持续异常，正在自愈重启...", SENDER_ID, logger)
                await asyncio.sleep(2)
                os.execv(sys.executable, [sys.executable, __file__])
        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 已退出")
