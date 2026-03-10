import asyncio
import sqlite3
import os
import time
import re
import logging
import tempfile
import shutil
from logging.handlers import RotatingFileHandler

from config import (
    BRIDGE_SECRET, SENDER_IDS, SENDER_ID,
    DB_PATH, LOG_DIR, CLI_PATHS,
    DEFAULT_MODEL, TASK_TIMEOUT, CHUNK_SIZE, ROBUST_PATH,
)

# ── 日志 ──────────────────────────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
_fh  = RotatingFileHandler(os.path.join(LOG_DIR, 'bridge.log'), maxBytes=5*1024*1024, backupCount=2)
_fh.setFormatter(_fmt)
_ch  = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger = logging.getLogger("ClaudeBridge")
logger.setLevel(logging.DEBUG)
logger.addHandler(_fh)
logger.addHandler(_ch)

# ── 状态 ──────────────────────────────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.is_running        = False
        self.current_process   = None
        self.last_message_date = 0
        self.task_start_time   = 0.0
        self.selected_model    = DEFAULT_MODEL
        self.task_queue        = asyncio.Queue()

app_state = AppState()

# ── 工具函数 ──────────────────────────────────────────────────────────────────
def decode_attributed_body(data: bytes | None) -> str | None:
    """从 iMessage attributedBody 二进制中提取纯文本（TypedStream/NSString 模式）"""
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
    """将 Markdown 格式转换为 iMessage 友好的纯文本"""
    text = re.sub(r'```\w*\n?', '---\n', text)
    text = re.sub(r'```', '---', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    return text


def verify_secret(content: str) -> tuple[bool, str]:
    """验证口令并返回 (通过, 实际内容)"""
    if not BRIDGE_SECRET:
        return True, content
    if content.startswith(BRIDGE_SECRET + " "):
        return True, content[len(BRIDGE_SECRET) + 1:].strip()
    return False, content

# ── 数据库 ────────────────────────────────────────────────────────────────────
def get_last_message() -> tuple[str | None, int | None]:
    """复制 chat.db（含 WAL）后读取最新消息，支持多账号，最多重试 3 次"""
    for attempt in range(3):
        tmp_db = tmp_wal = tmp_shm = None
        try:
            tmp_db  = tempfile.mktemp(suffix=".db",     prefix="chat_bridge_")
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
            cur.execute(f"""
                SELECT message.text, message.attributedBody, message.date, handle.id AS sender
                FROM message
                JOIN handle ON message.handle_id = handle.rowid
                WHERE handle.id IN ({placeholders}) AND message.is_from_me = 0
                ORDER BY message.date DESC LIMIT 1
            """, SENDER_IDS)
            row = cur.fetchone()
            conn.close()

            if row:
                text = row['text'] or decode_attributed_body(row['attributedBody'])
                return text, row['date']
            return None, None

        except sqlite3.DatabaseError as e:
            if "malformed" in str(e).lower() and attempt < 2:
                logger.warning(f"DB 读取尝试 {attempt+1} 失败，重试...")
                time.sleep(0.5)
                continue
            logger.error(f"DB 错误: {e}")
            return None, None
        except Exception as e:
            logger.error(f"DB 访问异常: {e}")
            return None, None
        finally:
            for f in [tmp_db, tmp_wal, tmp_shm]:
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
    return None, None

# ── iMessage 发送 ─────────────────────────────────────────────────────────────
async def send_imessage(message: str, recipient: str) -> None:
    safe = message.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
    script = f'tell application "Messages" to send "{safe}" to buddy "{recipient}"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception as e:
        logger.error(f"iMessage 发送失败: {e}")


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

# ── AI 任务执行 ───────────────────────────────────────────────────────────────
async def run_ai_task(model_type: str, content: str, recipient: str) -> None:
    app_state.is_running      = True
    app_state.task_start_time = time.time()
    logger.info(f"▶️ [{model_type}] {content}")

    try:
        path = CLI_PATHS.get(model_type)
        if not path or not os.path.exists(path):
            await send_imessage(f"❌ 未找到 {model_type} 路径: {path}", recipient)
            return

        if model_type == "claude":
            cmd = [path, "-p", content, "--continue"]
        elif model_type == "codex":
            cmd = [path, "exec", content, "--skip-git-repo-check", "--full-auto"]
        elif model_type == "gemini":
            cmd = [path, "-p", content]
        else:
            cmd = [path, content]

        env          = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["TERM"]     = "dumb"
        env["PATH"]     = f"{ROBUST_PATH}:{env.get('PATH', '')}"

        app_state.current_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                app_state.current_process.communicate(),
                timeout=TASK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            if app_state.current_process:
                app_state.current_process.kill()
            await send_imessage(f"⚠️ {model_type} 任务超时（>{TASK_TIMEOUT}s）", recipient)
            return

        if app_state.current_process.returncode in (-15, -9):
            return  # 用户主动中断，不回复

        raw = stdout.decode('utf-8', errors='ignore') if stdout else \
              stderr.decode('utf-8', errors='ignore') if stderr else ""
        output = strip_ansi(raw).strip()

        if output:
            await send_chunked_message(output, recipient, model_type)
        else:
            await send_imessage(f"⚠️ {model_type} 返回了空结果", recipient)

    except Exception as e:
        logger.error(f"执行异常: {e}")
        await send_imessage(f"⚠️ 脚本异常: {e}", recipient)
    finally:
        app_state.is_running      = False
        app_state.current_process = None

# ── 任务队列消费者 ─────────────────────────────────────────────────────────────
async def queue_worker() -> None:
    while True:
        model, content, recipient = await app_state.task_queue.get()
        await run_ai_task(model, content, recipient)
        app_state.task_queue.task_done()

# ── 主循环 ────────────────────────────────────────────────────────────────────
async def main() -> None:
    if not SENDER_IDS:
        logger.error("❌ SENDER_IDS 未配置，请检查 .env 文件")
        return

    logger.info(f"🚀 启动！默认模型: {app_state.selected_model} | 安全口令: {'已启用' if BRIDGE_SECRET else '⚠️ 未启用'}")

    asyncio.create_task(queue_worker())

    _, last_date = get_last_message()
    app_state.last_message_date = last_date or 0

    while True:
        try:
            content, msg_date = get_last_message()

            if msg_date and msg_date > app_state.last_message_date:
                app_state.last_message_date = msg_date

                if not content:
                    await asyncio.sleep(1)
                    continue

                content = content.strip()
                logger.info(f"📥 {content}")

                # ── 口令验证 ──────────────────────────────────
                ok, content = verify_secret(content)
                if not ok:
                    await send_imessage("🔒 未授权", SENDER_ID)
                    await asyncio.sleep(1)
                    continue

                # ── 模型切换指令 ──────────────────────────────
                if content.lower() == "/c":
                    app_state.selected_model = "claude"
                    await send_imessage("✅ 已切换至 Claude Code", SENDER_ID)
                    await asyncio.sleep(1)
                    continue
                elif content.lower() == "/g":
                    app_state.selected_model = "gemini"
                    await send_imessage("✅ 已切换至 Gemini", SENDER_ID)
                    await asyncio.sleep(1)
                    continue
                elif content.lower() == "/x":
                    app_state.selected_model = "codex"
                    await send_imessage("✅ 已切换至 Codex", SENDER_ID)
                    await asyncio.sleep(1)
                    continue

                # ── 系统指令 ──────────────────────────────────
                if content.startswith("/"):
                    if content == "/ping":
                        await send_imessage("🏓 Pong!", SENDER_ID)
                    elif content == "/status":
                        msg = f"🤖 当前模型: {app_state.selected_model.upper()}\n"
                        if app_state.is_running:
                            elapsed = int(time.time() - app_state.task_start_time)
                            msg += f"⏳ 执行中... ({elapsed}s)\n"
                        else:
                            msg += "💤 空闲\n"
                        msg += f"📋 队列: {app_state.task_queue.qsize()} 条待处理"
                        await send_imessage(msg, SENDER_ID)
                    elif content == "/stop":
                        proc = app_state.current_process
                        if proc and app_state.is_running:
                            proc.terminate()
                            await send_imessage("🛑 已中断当前任务", SENDER_ID)
                        else:
                            await send_imessage("💤 当前没有运行中的任务", SENDER_ID)
                    elif content == "/help":
                        await send_imessage(
                            "📖 指令列表：\n"
                            "/c  → Claude Code\n"
                            "/g  → Gemini\n"
                            "/x  → Codex\n"
                            "/status → 状态\n"
                            "/stop   → 中断任务\n"
                            "/ping   → 心跳检测\n"
                            "/help   → 本帮助",
                            SENDER_ID,
                        )
                    else:
                        await send_imessage(f"❓ 未知指令: {content}，输入 /help 查看列表", SENDER_ID)
                    await asyncio.sleep(1)
                    continue

                # ── 入队执行 ──────────────────────────────────
                await app_state.task_queue.put((app_state.selected_model, content, SENDER_ID))
                queue_size = app_state.task_queue.qsize()
                if queue_size > 1:
                    await send_imessage(f"📋 已加入队列（前方还有 {queue_size-1} 条）", SENDER_ID)

        except Exception as e:
            logger.error(f"主循环异常: {e}")

        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 已退出")
