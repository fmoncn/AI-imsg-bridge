import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass


@dataclass
class IncomingMessage:
    rowid: int
    text: str | None
    date: int
    attachment: str | None
    sender: str


def decode_attributed_body(data: bytes | None) -> str | None:
    if not data:
        return None
    try:
        marker = b"NSString"
        idx = data.find(marker)
        if idx == -1:
            return None
        pos = data.find(b"\x2B", idx + len(marker))
        if pos == -1:
            return None
        length_byte = data[pos + 1]
        if length_byte == 0x81:
            length = data[pos + 2]
            content = data[pos + 3: pos + 3 + length]
        else:
            length = length_byte
            content = data[pos + 2: pos + 2 + length]
        text = content.decode("utf-8", errors="ignore").strip()
        return text or None
    except Exception:
        return None


def _make_temp_db() -> tuple[str, str, str]:
    fd, tmp_db = tempfile.mkstemp(suffix=".db", prefix="chat_bridge_")
    os.close(fd)
    return tmp_db, tmp_db + "-wal", tmp_db + "-shm"


def _cleanup_temp_files(*paths: str) -> None:
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def _copy_message_db(db_path: str) -> tuple[str, str, str]:
    tmp_db, tmp_wal, tmp_shm = _make_temp_db()
    shutil.copy(db_path, tmp_db)
    if os.path.exists(db_path + "-wal"):
        shutil.copy(db_path + "-wal", tmp_wal)
    if os.path.exists(db_path + "-shm"):
        shutil.copy(db_path + "-shm", tmp_shm)
    return tmp_db, tmp_wal, tmp_shm


def get_latest_marker(db_path: str, sender_ids: list[str], logger) -> tuple[int, int]:
    messages = fetch_new_messages(db_path, sender_ids, 0, 0, logger, limit=1, descending=True)
    if not messages:
        return 0, 0
    latest = messages[0]
    return latest.date, latest.rowid


def fetch_new_messages(
    db_path: str,
    sender_ids: list[str],
    last_date: int,
    last_rowid: int,
    logger,
    limit: int | None = None,
    descending: bool = False,
) -> list[IncomingMessage]:
    if not sender_ids:
        return []

    for attempt in range(3):
        tmp_db = tmp_wal = tmp_shm = None
        try:
            tmp_db, tmp_wal, tmp_shm = _copy_message_db(db_path)
            conn = sqlite3.connect(tmp_db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            placeholders = ", ".join(["?"] * len(sender_ids))
            order = "DESC" if descending else "ASC"
            limit_sql = f"LIMIT {limit}" if limit else ""
            params = sender_ids + [last_date, last_date, last_rowid]

            cur.execute(
                f"""
                SELECT message.rowid, message.text, message.attributedBody, message.date, handle.id AS sender
                FROM message
                JOIN handle ON message.handle_id = handle.rowid
                WHERE handle.id IN ({placeholders})
                  AND message.is_from_me = 0
                  AND (message.date > ? OR (message.date = ? AND message.rowid > ?))
                ORDER BY message.date {order}, message.rowid {order}
                {limit_sql}
                """,
                params,
            )
            rows = cur.fetchall()
            results: list[IncomingMessage] = []
            for row in rows:
                text = row["text"] or decode_attributed_body(row["attributedBody"])
                cur.execute(
                    """
                    SELECT attachment.filename
                    FROM attachment
                    JOIN message_attachment_join ON attachment.rowid = message_attachment_join.attachment_id
                    WHERE message_attachment_join.message_id = ?
                      AND attachment.mime_type LIKE 'image/%'
                    LIMIT 1
                    """,
                    (row["rowid"],),
                )
                att_row = cur.fetchone()
                results.append(
                    IncomingMessage(
                        rowid=row["rowid"],
                        text=text,
                        date=row["date"],
                        attachment=att_row["filename"] if att_row else None,
                        sender=row["sender"],
                    )
                )
            conn.close()
            return results
        except sqlite3.DatabaseError as exc:
            if "malformed" in str(exc).lower() and attempt < 2:
                logger.warning(f"DB 读取尝试 {attempt + 1} 失败，重试...")
                time.sleep(0.5)
                continue
            logger.error(f"DB 错误: {exc}")
            return []
        except Exception as exc:
            logger.error(f"DB 访问异常: {exc}")
            return []
        finally:
            _cleanup_temp_files(tmp_db, tmp_wal, tmp_shm)
    return []
