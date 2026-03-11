import asyncio
import sqlite3
import os
import signal
import time
import re
import json
import logging
import tempfile
import shutil
import subprocess
import aiohttp
from logging.handlers import RotatingFileHandler

from config import (
    BRIDGE_SECRET, SENDER_IDS, SENDER_ID,
    DB_PATH, LOG_DIR, CLI_PATHS,
    DEFAULT_MODEL, TASK_TIMEOUT, CHUNK_SIZE, ROBUST_PATH,
    MEMORY_TURNS, MEMORY_DIR,
    TAVILY_API_KEY, TAVILY_SEARCH_URL, PROGRESS_INTERVAL,
)

# ── 日志 ──────────────────────────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)
_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
_fh  = RotatingFileHandler(os.path.join(LOG_DIR, 'bridge.log'), maxBytes=5*1024*1024, backupCount=2)
_fh.setFormatter(_fmt)
_ch  = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger = logging.getLogger("ClaudeBridge")
logger.setLevel(logging.DEBUG)
logger.addHandler(_fh)
logger.addHandler(_ch)

# ── 对话记忆 ──────────────────────────────────────────────────────────────────
class ConversationMemory:
    def __init__(self):
        self._history: dict[str, list[dict]] = {}
        self._has_session: dict[str, bool]   = {}
        self._load_all()

    def _path(self, model: str) -> str:
        return os.path.join(MEMORY_DIR, f"{model}.json")

    def _load_all(self):
        for model in ("claude", "gemini", "codex"):
            p = self._path(model)
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        data = json.load(f)
                    self._history[model]     = data.get("history", [])
                    self._has_session[model] = data.get("has_session", False)
                    logger.info(f"📚 [{model}] 加载历史 {len(self._history[model])} 条")
                except Exception as e:
                    logger.warning(f"加载 {model} 历史失败: {e}")
                    self._history[model]     = []
                    self._has_session[model] = False
            else:
                self._history[model]     = []
                self._has_session[model] = False

    def _save(self, model: str):
        try:
            with open(self._path(model), "w") as f:
                json.dump({"history": self._history[model], "has_session": self._has_session[model]},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存 {model} 历史失败: {e}")

    def add(self, model: str, role: str, content: str):
        if model not in self._history:
            self._history[model] = []
        self._history[model].append({"role": role, "content": content, "ts": time.time()})
        if len(self._history[model]) > MEMORY_TURNS * 2:
            self._history[model] = self._history[model][-(MEMORY_TURNS * 2):]
        if role == "assistant":
            self._has_session[model] = True
        self._save(model)

    def get_context(self, model: str) -> str:
        history = self._history.get(model, [])
        if not history:
            return ""
        lines = ["[对话历史]"]
        for msg in history:
            prefix = "用户" if msg["role"] == "user" else "AI"
            lines.append(f"{prefix}: {msg['content']}")
        lines.append("[以上是历史对话，请继续]\n")
        return "\n".join(lines)

    def has_session(self, model: str) -> bool:
        return self._has_session.get(model, False)

    def reset(self, model: str):
        self._history[model]     = []
        self._has_session[model] = False
        self._save(model)
        logger.info(f"🗑️ [{model}] 对话历史已清空")

    def reset_all(self):
        for model in list(self._history.keys()):
            self.reset(model)

    def summary(self, model: str) -> str:
        history = self._history.get(model, [])
        turns   = len([m for m in history if m["role"] == "user"])
        if not history:
            return "无历史记录"
        age_min = int((time.time() - history[-1]["ts"]) / 60)
        return f"{turns} 轮对话，最后活跃 {age_min} 分钟前"

memory = ConversationMemory()

# ── 状态 ──────────────────────────────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.is_running            = False
        self.current_process       = None
        self.last_message_date     = 0
        self.task_start_time       = 0.0
        self.start_time            = time.time()   # bridge 启动时间（运行时长用）
        self.selected_model        = DEFAULT_MODEL
        self.task_queue            = asyncio.Queue()
        self.gemini_timeout_count  = 0  # 连续超时次数（>=2 自动重置历史）
        self.db_error_count        = 0  # 连续 DB 异常次数（>=5 自愈重启）

app_state = AppState()

# ── 模型健康追踪 ───────────────────────────────────────────────────────────────
_QUOTA_PATTERN = re.compile(
    r'quota|rate.?limit|429|resource.?exhausted|too.?many.?request|limit.?exceed|'
    r'billing|payment|subscription|insufficient|capacity',
    re.IGNORECASE,
)
_FALLBACK_CHAIN = {
    "gemini": ["claude", "codex"],
    "claude": ["gemini", "codex"],
    "codex":  ["claude", "gemini"],
}

class ModelHealth:
    def __init__(self):
        self._success:       dict[str, int]   = {m: 0 for m in ("claude", "gemini", "codex")}
        self._failure:       dict[str, int]   = {m: 0 for m in ("claude", "gemini", "codex")}
        self._last_error:    dict[str, str]   = {}
        self._disabled_until:dict[str, float] = {}

    def is_quota_error(self, text: str) -> bool:
        return bool(_QUOTA_PATTERN.search(text))

    def is_available(self, model: str) -> bool:
        until = self._disabled_until.get(model, 0)
        if time.time() < until:
            return False
        if model in self._disabled_until:
            del self._disabled_until[model]  # 自动解禁
        return True

    def record_success(self, model: str):
        self._success[model] = self._success.get(model, 0) + 1
        self._last_error.pop(model, None)

    def record_failure(self, model: str, reason: str, quota: bool = False):
        self._failure[model] = self._failure.get(model, 0) + 1
        self._last_error[model] = reason[:40]
        if quota:
            self._disabled_until[model] = time.time() + 3600  # 禁用 1 小时
            logger.warning(f"⛔ [{model}] 配额耗尽，禁用 1 小时")

    def get_fallback(self, failed_model: str) -> str | None:
        for m in _FALLBACK_CHAIN.get(failed_model, []):
            if self.is_available(m):
                return m
        return None

    def success_rate(self, model: str) -> str:
        s = self._success.get(model, 0)
        f = self._failure.get(model, 0)
        total = s + f
        if total == 0:
            return "无数据"
        return f"{int(s / total * 100)}%"

    def status_line(self, model: str) -> str:
        if not self.is_available(model):
            remain = int((self._disabled_until.get(model, 0) - time.time()) / 60)
            return f"⛔ 配额耗尽（{remain}分钟后恢复）"
        f = self._failure.get(model, 0)
        rate = self.success_rate(model)
        last = self._last_error.get(model)
        if f >= 3 and last:
            return f"⚠️ 不稳定 | 成功率 {rate} | 最近: {last}"
        return f"✅ 正常 | 成功率 {rate}"

health = ModelHealth()

# ── 工具函数 ──────────────────────────────────────────────────────────────────
def decode_attributed_body(data: bytes | None) -> str | None:
    if not data:
        return None
    try:
        marker = b'NSString'
        idx = data.find(marker)
        if idx == -1:
            return None
        pos = data.find(b'\x2B', idx + len(marker))
        if pos == -1:
            return None
        length_byte = data[pos + 1]
        if length_byte == 0x81:
            length  = data[pos + 2]
            content = data[pos + 3: pos + 3 + length]
        else:
            length  = length_byte
            content = data[pos + 2: pos + 2 + length]
        text = content.decode('utf-8', errors='ignore').strip()
        return text or None
    except Exception as e:
        logger.error(f"解析 attributedBody 失败: {e}")
        return None


def strip_ansi(text: str) -> str:
    return re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)


def normalize_markdown(text: str) -> str:
    text = re.sub(r'```\w*\n?', '---\n', text)
    text = re.sub(r'```', '---', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    return text


def verify_secret(content: str) -> tuple[bool, str]:
    if not BRIDGE_SECRET:
        return True, content
    if content.startswith(BRIDGE_SECRET + " "):
        return True, content[len(BRIDGE_SECRET) + 1:].strip()
    return False, content

# ── Tavily 联网搜索 ───────────────────────────────────────────────────────────
_SEARCH_KEYWORDS = re.compile(
    r'最新|今天|现在|新闻|价格|股价|天气|多少|哪里|什么时候|最近|昨天|明天'
    r'|今年|本周|上周|实时|现价|汇率|涨跌|发布|上市|热点|趋势|排行'
    r'|latest|today|now|news|price|weather|current|recent|trending',
    re.IGNORECASE,
)

def should_search(content: str) -> bool:
    return bool(_SEARCH_KEYWORDS.search(content)) and bool(TAVILY_API_KEY)

async def tavily_search(query: str) -> str | None:
    """调用 Tavily API，返回格式化的搜索摘要"""
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "api_key":     TAVILY_API_KEY,
                "query":       query,
                "max_results": 5,
                "search_depth": "basic",
            }
            async with session.post(TAVILY_SEARCH_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"Tavily 返回 {resp.status}")
                    return None
                data = await resp.json()

        results = data.get("results", [])
        if not results:
            return None

        lines = ["[联网搜索结果]"]
        for i, r in enumerate(results[:5], 1):
            lines.append(f"{i}. {r.get('title', '')}")
            lines.append(f"   {r.get('content', '')[:200]}")
        lines.append("[以上为实时搜索结果，请结合回答]\n")
        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"Tavily 搜索失败: {e}")
        return None

# ── 图片处理 ──────────────────────────────────────────────────────────────────
def prepare_image(raw_path: str) -> str | None:
    """展开路径，HEIC 转 JPEG，返回可用路径"""
    path = os.path.expanduser(raw_path)
    if not os.path.exists(path):
        logger.warning(f"附件不存在: {path}")
        return None

    ext = os.path.splitext(path)[1].lower()
    if ext in (".heic", ".heif"):
        try:
            jpg_path = tempfile.mktemp(suffix=".jpg", prefix="bridge_img_")
            subprocess.run(
                ["sips", "-s", "format", "jpeg", path, "--out", jpg_path],
                check=True, capture_output=True,
            )
            logger.info(f"🖼️ HEIC→JPEG: {jpg_path}")
            return jpg_path
        except Exception as e:
            logger.warning(f"图片转换失败: {e}")
            return None

    return path  # PNG / GIF / JPEG 直接返回


# ── 孤儿进程清理 ──────────────────────────────────────────────────────────────
def kill_orphan_processes() -> list[int]:
    """启动时清理上次 bridge 崩溃留下的 CLI 子进程"""
    killed = []
    cli_paths = [p for p in CLI_PATHS.values() if p]
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            for cli_path in cli_paths:
                if cli_path in line:
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            pid = int(parts[1])
                            if pid != os.getpid():
                                os.kill(pid, signal.SIGTERM)
                                killed.append(pid)
                                logger.info(f"🧹 清理孤儿进程 PID={pid}")
                        except (ValueError, ProcessLookupError):
                            pass
    except Exception as e:
        logger.warning(f"孤儿进程清理失败: {e}")
    return killed


# ── stderr 日志轮转 ───────────────────────────────────────────────────────────
def rotate_stderr_log() -> None:
    """启动时检查 launch_stderr.log，超过 5MB 则截断保留最后 1MB"""
    stderr_log = os.path.join(LOG_DIR, "launch_stderr.log")
    max_size   = 5 * 1024 * 1024
    keep_size  = 1 * 1024 * 1024
    try:
        if os.path.exists(stderr_log) and os.path.getsize(stderr_log) > max_size:
            with open(stderr_log, "rb") as f:
                f.seek(-keep_size, 2)
                tail = f.read()
            with open(stderr_log, "wb") as f:
                f.write(b"[... truncated at startup, keeping last 1MB ...]\n")
                f.write(tail)
            logger.info("📋 launch_stderr.log 已截断（超过 5MB）")
    except Exception as e:
        logger.warning(f"stderr 日志截断失败: {e}")


# ── 数据库（含附件查询）─────────────────────────────────────────────────────
def get_last_message() -> tuple[str | None, int | None, str | None]:
    """返回 (文本内容, 日期戳, 附件路径 or None)"""
    for attempt in range(3):
        tmp_db = tmp_wal = tmp_shm = None
        try:
            tmp_db  = tempfile.mktemp(suffix=".db",  prefix="chat_bridge_")
            tmp_wal = tmp_db + "-wal"
            tmp_shm = tmp_db + "-shm"

            shutil.copy(DB_PATH, tmp_db)
            if os.path.exists(DB_PATH + "-wal"):
                shutil.copy(DB_PATH + "-wal", tmp_wal)
            if os.path.exists(DB_PATH + "-shm"):
                shutil.copy(DB_PATH + "-shm", tmp_shm)

            conn = sqlite3.connect(tmp_db)
            conn.row_factory = sqlite3.Row
            cur  = conn.cursor()
            placeholders = ', '.join(['?'] * len(SENDER_IDS))

            # 主消息
            cur.execute(f"""
                SELECT message.rowid, message.text, message.attributedBody,
                       message.date, handle.id AS sender
                FROM message
                JOIN handle ON message.handle_id = handle.rowid
                WHERE handle.id IN ({placeholders}) AND message.is_from_me = 0
                ORDER BY message.date DESC LIMIT 1
            """, SENDER_IDS)
            row = cur.fetchone()

            if not row:
                conn.close()
                return None, None, None

            text        = row['text'] or decode_attributed_body(row['attributedBody'])
            msg_date    = row['date']
            msg_rowid   = row['rowid']

            # 查附件
            cur.execute("""
                SELECT attachment.filename, attachment.mime_type
                FROM attachment
                JOIN message_attachment_join ON attachment.rowid = message_attachment_join.attachment_id
                WHERE message_attachment_join.message_id = ?
                  AND attachment.mime_type LIKE 'image/%'
                LIMIT 1
            """, (msg_rowid,))
            att_row    = cur.fetchone()
            attachment = att_row['filename'] if att_row else None

            conn.close()
            return text, msg_date, attachment

        except sqlite3.DatabaseError as e:
            if "malformed" in str(e).lower() and attempt < 2:
                logger.warning(f"DB 读取尝试 {attempt+1} 失败，重试...")
                time.sleep(0.5)
                continue
            logger.error(f"DB 错误: {e}")
            return None, None, None
        except Exception as e:
            logger.error(f"DB 访问异常: {e}")
            return None, None, None
        finally:
            for f in [tmp_db, tmp_wal, tmp_shm]:
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
    return None, None, None

async def send_imessage(message: str, recipient: str) -> None:
    safe = message.replace('\\', '\\\\').replace('"', '\\"')
    script = f'tell application "Messages" to send "{safe}" to buddy "{recipient}"'
    for attempt in range(3):
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("✅ iMessage 已发送")
                return
            err = stderr.decode('utf-8', errors='ignore').strip()
            logger.warning(f"iMessage 发送失败 (attempt {attempt+1}, code={proc.returncode}): {err}")
        except asyncio.TimeoutError:
            logger.warning(f"iMessage 发送超时 (attempt {attempt+1})")
        except Exception as e:
            logger.warning(f"iMessage 发送异常 (attempt {attempt+1}): {e}")
        if attempt < 2:
            await asyncio.sleep(2)
    logger.error("iMessage 发送彻底失败（3次均未成功）")


# ── 工程心跳 ──────────────────────────────────────────────────────────────────
async def heartbeat() -> None:
    """每 30 分钟自检 iMessage 链路，00:00–08:00 静默不打扰"""
    await asyncio.sleep(30 * 60)  # 启动后 30 分钟再首次检测
    while True:
        hour = time.localtime().tm_hour
        if 0 <= hour < 8:
            # 静默期：每 5 分钟再检查一次时间，直到 08:00
            await asyncio.sleep(5 * 60)
            continue
        try:
            ts = time.strftime("%H:%M")
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'tell application "Messages" to send "💓 Bridge 在线 | {ts}" to buddy "{SENDER_ID}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info(f"💓 心跳自检通过 ({ts})")
            else:
                err = stderr.decode('utf-8', errors='ignore').strip()
                logger.error(f"💓 心跳自检失败: {err}")
        except asyncio.TimeoutError:
            logger.error("💓 心跳自检超时（>15s）")
        except Exception as e:
            logger.error(f"💓 心跳自检异常: {e}")
        await asyncio.sleep(30 * 60)


async def send_chunked_message(text: str, recipient: str, model_name: str) -> None:
    if not text:
        return
    text   = normalize_markdown(text)
    header = f"【{model_name.upper()}】\n"
    full   = header + text

    if len(full) <= CHUNK_SIZE:
        await send_imessage(full, recipient)
        return

    chunks: list[str] = []
    current = header
    for line in text.split('\n'):
        if len(current) + len(line) + 1 > CHUNK_SIZE:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current)

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"({i}/{total})\n" if total > 1 else ""
        await send_imessage(prefix + chunk.strip(), recipient)
        await asyncio.sleep(0.5)

# ── 进度通知 ──────────────────────────────────────────────────────────────────
async def progress_notifier(model_type: str, recipient: str, stop_event: asyncio.Event):
    """每 PROGRESS_INTERVAL 秒发一条进度消息，直到任务完成"""
    await asyncio.sleep(PROGRESS_INTERVAL)
    while not stop_event.is_set():
        elapsed = int(time.time() - app_state.task_start_time)
        await send_imessage(f"⏳ {model_type.upper()} 思考中... ({elapsed}s)", recipient)
        await asyncio.sleep(PROGRESS_INTERVAL)

# ── 任务超时分级 ──────────────────────────────────────────────────────────────
_CODE_KEYWORDS = re.compile(
    r'代码|程序|函数|脚本|写|开发|实现|调试|debug|fix|bug|code|script|function|class|api',
    re.IGNORECASE,
)

def get_task_timeout(content: str, has_search: bool) -> int:
    if _CODE_KEYWORDS.search(content):
        return 300
    if has_search:
        return 90
    return 60

# ── AI 任务执行 ───────────────────────────────────────────────────────────────
async def run_ai_task(model_type: str, content: str, recipient: str,
                      attachment: str | None = None,
                      restore_model: str | None = None) -> None:
    app_state.is_running      = True
    app_state.task_start_time = time.time()
    converted_img             = None
    logger.info(f"▶️ [{model_type}] {content}" + (f" [图片]" if attachment else ""))

    # ── 1. 联网搜索增强 ────────────────────────────────────────────────────────
    search_prefix = ""
    has_search = should_search(content)
    if has_search:
        logger.info(f"🔍 触发联网搜索: {content[:50]}")
        await send_imessage("🔍 正在联网搜索...", recipient)
        search_result = await tavily_search(content)
        if search_result:
            search_prefix = search_result
            logger.info("✅ 搜索结果已附加")

    # ── 2. 记录用户消息 ────────────────────────────────────────────────────────
    memory.add(model_type, "user", content)

    # ── 3. 启动进度通知 ────────────────────────────────────────────────────────
    stop_event    = asyncio.Event()
    progress_task = asyncio.create_task(
        progress_notifier(model_type, recipient, stop_event)
    )

    try:
        path = CLI_PATHS.get(model_type)
        if not path or not os.path.exists(path):
            await send_imessage(f"❌ 未找到 {model_type} 路径: {path}", recipient)
            return

        # ── 4. 构建 prompt ────────────────────────────────────────────────────
        # 处理图片附件
        img_note = ""
        if attachment:
            converted_img = prepare_image(attachment)
            if converted_img:
                img_note = f"[用户发送了图片: {converted_img}]\n"
                logger.info(f"🖼️ 附件就绪: {converted_img}")

        full_content = f"{search_prefix}{img_note}{content}"

        env             = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["TERM"]     = "dumb"
        env["PATH"]     = f"{ROBUST_PATH}:{env.get('PATH', '')}"

        # ── 5. 构建命令（含记忆逻辑）─────────────────────────────────────────
        if model_type == "claude":
            if memory.has_session("claude"):
                cmd = [path, "-p", full_content, "--continue"]
            else:
                cmd = [path, "-p", full_content]

        elif model_type == "gemini":
            if memory.has_session("gemini"):
                # 先用 30s 短超时探测 --resume，失败则降级为新会话
                probe_cmd = [path, "-y", "-p", full_content, "--resume", "latest"]
                app_state.current_process = await asyncio.create_subprocess_exec(
                    *probe_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        app_state.current_process.communicate(), timeout=30
                    )
                    if app_state.current_process.returncode not in (-15, -9):
                        raw    = stdout.decode('utf-8', errors='ignore') if stdout else \
                                 stderr.decode('utf-8', errors='ignore') if stderr else ""
                        output = strip_ansi(raw).strip()
                        app_state.gemini_timeout_count = 0
                        if output:
                            memory.add(model_type, "assistant", output[:500])
                            await send_chunked_message(output, recipient, model_type)
                        else:
                            await send_imessage("⚠️ gemini 返回了空结果", recipient)
                        return
                except asyncio.TimeoutError:
                    if app_state.current_process:
                        app_state.current_process.kill()
                    app_state.gemini_timeout_count += 1
                    logger.warning(f"Gemini --resume 探测超时（第{app_state.gemini_timeout_count}次）")
                    if app_state.gemini_timeout_count >= 2:
                        memory.reset("gemini")
                        app_state.gemini_timeout_count = 0
                        logger.warning("Gemini 连续超时 2 次，已自动重置历史")
                        await send_imessage("⚠️ Gemini 会话异常，已自动重置历史，重新开始", recipient)
                    else:
                        await send_imessage("⚠️ Gemini 会话恢复超时，降级为新会话重试", recipient)
                    cmd = [path, "-y", "-p", full_content]
                else:
                    cmd = [path, "-y", "-p", full_content]
            else:
                cmd = [path, "-y", "-p", full_content]

        elif model_type == "codex":
            ctx         = memory.get_context("codex")
            full_prompt = f"{ctx}{full_content}" if ctx else full_content
            cmd = [path, "exec", full_prompt, "--skip-git-repo-check", "--full-auto"]

        else:
            cmd = [path, full_content]

        app_state.current_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        timeout = get_task_timeout(content, has_search)
        try:
            stdout, stderr = await asyncio.wait_for(
                app_state.current_process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            if app_state.current_process:
                app_state.current_process.kill()
            health.record_failure(model_type, "timeout")
            await send_imessage(f"⚠️ {model_type} 任务超时（>{timeout}s）", recipient)
            return

        if app_state.current_process.returncode in (-15, -9):
            return

        raw    = stdout.decode('utf-8', errors='ignore') if stdout else \
                 stderr.decode('utf-8', errors='ignore') if stderr else ""
        output = strip_ansi(raw).strip()

        if output:
            # ── 配额耗尽检测 → 互救 ───────────────────────────────────────
            if health.is_quota_error(output):
                health.record_failure(model_type, "quota exhausted", quota=True)
                fallback = health.get_fallback(model_type)
                if fallback:
                    logger.warning(f"🔄 [{model_type}] 配额耗尽，启动互救 → {fallback}")
                    await send_imessage(
                        f"⚠️ {model_type.upper()} 配额耗尽\n🔄 自动切换 {fallback.upper()} 重试...",
                        recipient,
                    )
                    await run_ai_task(fallback, content, recipient, attachment)
                else:
                    await send_imessage("⛔ 所有模型配额耗尽，请稍后重试", recipient)
                return
            health.record_success(model_type)
            memory.add(model_type, "assistant", output[:500])
            await send_chunked_message(output, recipient, model_type)
        else:
            health.record_failure(model_type, "empty response")
            await send_imessage(f"⚠️ {model_type} 返回了空结果", recipient)

    except Exception as e:
        health.record_failure(model_type, str(e)[:40])
        logger.error(f"执行异常: {e}")
        await send_imessage(f"⚠️ 脚本异常: {e}", recipient)
    finally:
        stop_event.set()
        progress_task.cancel()
        app_state.is_running      = False
        app_state.current_process = None
        # 图片路由：恢复原模型
        if restore_model and app_state.selected_model != restore_model:
            app_state.selected_model = restore_model
            await send_imessage(f"🔄 已恢复至 {restore_model.upper()}", recipient)
        # 清理临时转换的图片
        if converted_img and converted_img != attachment:
            try:
                os.remove(converted_img)
            except Exception:
                pass

# ── 任务队列消费者 ─────────────────────────────────────────────────────────────
async def queue_worker() -> None:
    while True:
        item = await app_state.task_queue.get()
        model, content, recipient, attachment = item[:4]
        restore_model = item[4] if len(item) > 4 else None
        await run_ai_task(model, content, recipient, attachment, restore_model)
        app_state.task_queue.task_done()

# ── 主循环 ────────────────────────────────────────────────────────────────────
async def main() -> None:
    logger.info(f"🚀 启动！默认模型: {app_state.selected_model} | "
                f"安全口令: {'已启用' if BRIDGE_SECRET else '⚠️ 未启用'} | "
                f"Tavily: {'✅' if TAVILY_API_KEY else '❌'}")
    for m in ("claude", "gemini", "codex"):
        logger.info(f"   [{m}] {memory.summary(m)}")

    asyncio.create_task(queue_worker())
    asyncio.create_task(heartbeat())

    # 启动清理
    rotate_stderr_log()
    killed = kill_orphan_processes()
    if killed:
        logger.info(f"🧹 已清理 {len(killed)} 个孤儿进程")

    if not SENDER_IDS:
        logger.error("❌ SENDER_IDS 未配置，请检查 .env 文件")
        return

    _, last_date, _ = get_last_message()
    app_state.last_message_date = last_date or 0

    # 启动通知
    await send_imessage(
        f"🚀 Bridge 已启动 | 模型: {app_state.selected_model.upper()} | {time.strftime('%H:%M')}",
        SENDER_ID,
    )

    while True:
        try:
            content, msg_date, attachment = get_last_message()
            app_state.db_error_count = 0  # 成功读取则重置计数

            if msg_date and msg_date > app_state.last_message_date:
                app_state.last_message_date = msg_date

                # 纯图片消息（无文字）也处理
                if not content and not attachment:
                    await asyncio.sleep(1)
                    continue

                content = (content or "").strip()
                if not content and attachment:
                    content = "请描述这张图片"

                logger.info(f"📥 {content}" + (f" [📎{os.path.basename(attachment)}]" if attachment else ""))

                # ── 口令验证 ──────────────────────────────────────────────────
                ok, content = verify_secret(content)
                if not ok:
                    await send_imessage("🔒 未授权", SENDER_ID)
                    await asyncio.sleep(1)
                    continue

                # ── 模型切换 ──────────────────────────────────────────────────
                if content.lower() == "/c":
                    app_state.selected_model = "claude"
                    await send_imessage(f"✅ 已切换至 Claude Code\n💬 {memory.summary('claude')}", SENDER_ID)
                    await asyncio.sleep(1)
                    continue
                elif content.lower() == "/g":
                    app_state.selected_model = "gemini"
                    await send_imessage(f"✅ 已切换至 Gemini\n💬 {memory.summary('gemini')}", SENDER_ID)
                    await asyncio.sleep(1)
                    continue
                elif content.lower() == "/x":
                    app_state.selected_model = "codex"
                    await send_imessage(f"✅ 已切换至 Codex\n💬 {memory.summary('codex')}", SENDER_ID)
                    await asyncio.sleep(1)
                    continue

                # ── 系统指令 ──────────────────────────────────────────────────
                if content.startswith("/"):
                    cmd_lower = content.lower()

                    if cmd_lower == "/ping":
                        await send_imessage("🏓 Pong!", SENDER_ID)

                    elif cmd_lower == "/status":
                        search_status = "✅ 已启用" if TAVILY_API_KEY else "❌ 未配置"
                        uptime_sec = int(time.time() - app_state.start_time)
                        h, r = divmod(uptime_sec, 3600)
                        m, s = divmod(r, 60)
                        uptime_str = f"{h}小时{m}分{s}秒" if h else f"{m}分{s}秒"
                        msg = (
                            f"🤖 当前模型: {app_state.selected_model.upper()}\n"
                            f"{'⏳ 执行中... (' + str(int(time.time()-app_state.task_start_time)) + 's)' if app_state.is_running else '💤 空闲'}\n"
                            f"⏱️ 运行时长: {uptime_str}\n"
                            f"📋 队列: {app_state.task_queue.qsize()} 条待处理\n"
                            f"💬 {app_state.selected_model}: {memory.summary(app_state.selected_model)}\n"
                            f"🔍 联网搜索: {search_status}\n"
                            f"📊 模型健康:\n"
                            + "\n".join(
                                f"{'▶' if m == app_state.selected_model else ' '} {m.upper()}: {health.status_line(m)}"
                                for m in ("claude", "gemini", "codex")
                            )
                        )
                        await send_imessage(msg, SENDER_ID)

                    elif cmd_lower == "/health":
                        lines = ["📊 模型健康详情："]
                        for m in ("claude", "gemini", "codex"):
                            mark = "▶" if m == app_state.selected_model else " "
                            lines.append(f"{mark} {m.upper()}: {health.status_line(m)}")
                        lines.append(f"\n🔄 互救链：")
                        for m, chain in _FALLBACK_CHAIN.items():
                            avail = [f for f in chain if health.is_available(f)]
                            lines.append(f"  {m.upper()} → {'/'.join(avail).upper() if avail else '无可用备援'}")
                        await send_imessage("\n".join(lines), SENDER_ID)

                    elif cmd_lower == "/stop":
                        proc = app_state.current_process
                        if proc and app_state.is_running:
                            proc.terminate()
                            await send_imessage("🛑 已中断当前任务", SENDER_ID)
                        else:
                            await send_imessage("💤 当前没有运行中的任务", SENDER_ID)

                    elif cmd_lower == "/reset":
                        memory.reset(app_state.selected_model)
                        await send_imessage(f"🗑️ 已清空 {app_state.selected_model.upper()} 对话历史", SENDER_ID)

                    elif cmd_lower == "/reset all":
                        memory.reset_all()
                        await send_imessage("🗑️ 已清空所有模型对话历史", SENDER_ID)

                    elif cmd_lower == "/memory":
                        lines = ["📚 对话记忆状态："]
                        for m in ("claude", "gemini", "codex"):
                            mark = "▶️" if m == app_state.selected_model else "  "
                            lines.append(f"{mark} {m.upper()}: {memory.summary(m)}")
                        await send_imessage("\n".join(lines), SENDER_ID)

                    elif cmd_lower == "/help":
                        await send_imessage(
                            "📖 指令列表：\n"
                            "/c  → Claude Code\n"
                            "/g  → Gemini\n"
                            "/x  → Codex\n"
                            "/status  → 状态 + 模型健康\n"
                            "/health  → 健康详情 + 互救链\n"
                            "/memory  → 记忆状态\n"
                            "/reset   → 清空当前模型历史\n"
                            "/reset all → 清空所有历史\n"
                            "/stop    → 中断任务\n"
                            "/ping    → 心跳检测\n"
                            "/help    → 本帮助",
                            SENDER_ID,
                        )
                    else:
                        await send_imessage(f"❓ 未知指令: {content}，输入 /help 查看列表", SENDER_ID)

                    await asyncio.sleep(1)
                    continue

                # ── 消息长度保护 ───────────────────────────────────────────
                MAX_MSG_LEN = 8000
                if len(content) > MAX_MSG_LEN:
                    await send_imessage(
                        f"⚠️ 消息过长（{len(content)} 字），已截取前 {MAX_MSG_LEN} 字处理", SENDER_ID
                    )
                    content = content[:MAX_MSG_LEN]

                # ── 图片自动路由 Gemini ────────────────────────────────────
                target_model  = app_state.selected_model
                restore_model = None
                if attachment and target_model != "gemini":
                    restore_model = target_model
                    target_model  = "gemini"
                    logger.info(f"🖼️ 图片消息自动路由至 Gemini（原模型: {restore_model}）")
                    await send_imessage(
                        f"🖼️ 图片已路由至 Gemini 处理，完成后恢复 {restore_model.upper()}", SENDER_ID
                    )

                # ── 入队执行 ──────────────────────────────────────────────
                await app_state.task_queue.put((target_model, content, SENDER_ID, attachment, restore_model))
                queue_size = app_state.task_queue.qsize()
                if queue_size > 1:
                    await send_imessage(f"📋 已加入队列（前方还有 {queue_size-1} 条）", SENDER_ID)

        except Exception as e:
            logger.error(f"主循环异常: {e}")
            app_state.db_error_count += 1
            if app_state.db_error_count >= 5:
                logger.error("连续 5 次异常，自愈重启进程...")
                await send_imessage("⚠️ Bridge 检测到持续异常，正在自愈重启...", SENDER_ID)
                await asyncio.sleep(2)
                os.execv(__file__, [__file__])

        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 已退出")
