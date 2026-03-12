import asyncio
import re


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def normalize_markdown(text: str) -> str:
    text = re.sub(r"```\w*\n?", "---\n", text)
    text = re.sub(r"```", "---", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    return text


async def send_imessage(message: str, recipient: str, logger) -> None:
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Messages" to send "{safe}" to buddy "{recipient}"'
    for attempt in range(3):
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("✅ iMessage 已发送")
                return
            err = stderr.decode("utf-8", errors="ignore").strip()
            logger.warning(f"iMessage 发送失败 (attempt {attempt + 1}, code={proc.returncode}): {err}")
        except asyncio.TimeoutError:
            logger.warning(f"iMessage 发送超时 (attempt {attempt + 1})")
        except Exception as exc:
            logger.warning(f"iMessage 发送异常 (attempt {attempt + 1}): {exc}")
        if attempt < 2:
            await asyncio.sleep(2)
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
