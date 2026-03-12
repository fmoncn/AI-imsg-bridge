import asyncio
import json
import os
import re
import time


UNDELIVERED_LOG_PATH = os.path.expanduser("~/.claude_bridge/undelivered_messages.jsonl")


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def normalize_markdown(text: str) -> str:
    text = re.sub(r"```\w*\n?", "---\n", text)
    text = re.sub(r"```", "---", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    return text


def build_osascript_command(message: str, recipient: str) -> list[str]:
    script = (
        "on run argv\n"
        "set msgText to item 1 of argv\n"
        "set buddyId to item 2 of argv\n"
        'tell application "Messages" to send msgText to buddy buddyId\n'
        "end run"
    )
    return ["osascript", "-e", script, message, recipient]


def persist_undelivered_message(message: str, recipient: str, reason: str) -> None:
    os.makedirs(os.path.dirname(UNDELIVERED_LOG_PATH), exist_ok=True)
    with open(UNDELIVERED_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": int(time.time()),
                    "recipient": recipient,
                    "reason": reason[:300],
                    "message": message[:4000],
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def load_undelivered_messages(limit: int = 10) -> list[dict]:
    if not os.path.exists(UNDELIVERED_LOG_PATH):
        return []
    rows: list[dict] = []
    with open(UNDELIVERED_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows[-limit:]


def clear_undelivered_messages() -> int:
    rows = load_undelivered_messages(limit=10000)
    if os.path.exists(UNDELIVERED_LOG_PATH):
        os.remove(UNDELIVERED_LOG_PATH)
    return len(rows)


async def send_imessage(message: str, recipient: str, logger) -> None:
    last_error = ""
    for attempt in range(3):
        try:
            proc = await asyncio.create_subprocess_exec(
                *build_osascript_command(message, recipient),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("✅ iMessage 已发送")
                return
            err = stderr.decode("utf-8", errors="ignore").strip()
            last_error = err or f"code={proc.returncode}"
            logger.warning(f"iMessage 发送失败 (attempt {attempt + 1}, code={proc.returncode}): {err}")
        except asyncio.TimeoutError:
            last_error = "timeout"
            logger.warning(f"iMessage 发送超时 (attempt {attempt + 1})")
        except Exception as exc:
            last_error = str(exc)
            logger.warning(f"iMessage 发送异常 (attempt {attempt + 1}): {exc}")
        if attempt < 2:
            await asyncio.sleep(2)
    persist_undelivered_message(message, recipient, last_error or "unknown")
    logger.error("iMessage 发送彻底失败（3次均未成功）")


async def send_chunked_message(
    text: str,
    recipient: str,
    model_name: str,
    chunk_size: int,
    logger,
) -> None:
    if not text:
        return
    text = normalize_markdown(text)
    header = f"【{model_name.upper()}】\n"
    full = header + text
    if len(full) <= chunk_size:
        await send_imessage(full, recipient, logger)
        return

    chunks: list[str] = []
    current = header
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > chunk_size:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current)

    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        prefix = f"({index}/{total})\n" if total > 1 else ""
        await send_imessage(prefix + chunk.strip(), recipient, logger)
        await asyncio.sleep(0.5)
