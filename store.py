import json
import os
import sqlite3
import time

from state import TaskRequest


class BridgeStore:
    def __init__(self, db_path: str, logger):
        self.db_path = db_path
        self.logger = logger
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS channel_offsets (
                channel_id TEXT PRIMARY KEY,
                last_message_date INTEGER NOT NULL DEFAULT 0,
                last_message_rowid INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_bindings (
                channel_id TEXT PRIMARY KEY,
                selected_model TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_rowid INTEGER,
                recipient TEXT NOT NULL,
                model TEXT NOT NULL,
                task_kind TEXT NOT NULL DEFAULT 'task',
                content TEXT NOT NULL,
                attachment TEXT,
                status TEXT NOT NULL,
                force_search INTEGER NOT NULL DEFAULT 0,
                disable_search INTEGER NOT NULL DEFAULT 0,
                restore_model TEXT,
                output_excerpt TEXT,
                review_group_id TEXT,
                review_target_task_id INTEGER,
                review_role TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS review_groups (
                group_id TEXT PRIMARY KEY,
                target_task_id INTEGER NOT NULL,
                recipient TEXT NOT NULL,
                total_reviews INTEGER NOT NULL,
                summary_sent INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_confirmations (
                channel_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self._ensure_column(cur, "tasks", "task_kind", "TEXT NOT NULL DEFAULT 'task'")
        self._ensure_column(cur, "tasks", "output_excerpt", "TEXT")
        self._ensure_column(cur, "tasks", "review_group_id", "TEXT")
        self._ensure_column(cur, "tasks", "review_target_task_id", "INTEGER")
        self._ensure_column(cur, "tasks", "review_role", "TEXT")
        conn.commit()
        conn.close()

    def _ensure_column(self, cur, table: str, column: str, spec: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

    def get_offset(self, channel_id: str) -> tuple[int, int]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_message_date, last_message_rowid FROM channel_offsets WHERE channel_id = ?",
            (channel_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return 0, 0
        return int(row["last_message_date"]), int(row["last_message_rowid"])

    def set_offset(self, channel_id: str, last_message_date: int, last_message_rowid: int) -> None:
        now = time.time()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO channel_offsets(channel_id, last_message_date, last_message_rowid, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                last_message_date = excluded.last_message_date,
                last_message_rowid = excluded.last_message_rowid,
                updated_at = excluded.updated_at
            """,
            (channel_id, last_message_date, last_message_rowid, now),
        )
        conn.commit()
        conn.close()

    def get_selected_model(self, channel_id: str, default_model: str) -> str:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT selected_model FROM channel_bindings WHERE channel_id = ?", (channel_id,))
        row = cur.fetchone()
        conn.close()
        return row["selected_model"] if row else default_model

    def set_selected_model(self, channel_id: str, model: str) -> None:
        now = time.time()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO channel_bindings(channel_id, selected_model, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                selected_model = excluded.selected_model,
                updated_at = excluded.updated_at
            """,
            (channel_id, model, now),
        )
        conn.commit()
        conn.close()

    def create_task(self, task: TaskRequest, status: str = "queued", task_kind: str | None = None) -> int:
        now = time.time()
        kind = task_kind or task.task_kind
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks(
                message_rowid, recipient, model, task_kind, content, attachment, status,
                force_search, disable_search, restore_model, output_excerpt,
                review_group_id, review_target_task_id, review_role, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.rowid,
                task.recipient,
                task.model,
                kind,
                task.content,
                task.attachment,
                status,
                int(task.force_search),
                int(task.disable_search),
                task.restore_model,
                None,
                task.review_group_id,
                task.review_target_task_id,
                task.review_role,
                now,
                now,
            ),
        )
        task_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return task_id

    def update_task_result(self, task_id: int | None, output_excerpt: str) -> None:
        if not task_id:
            return
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET output_excerpt = ?, updated_at = ? WHERE id = ?",
            (output_excerpt[:1000], time.time(), task_id),
            )
        conn.commit()
        conn.close()

    def create_review_group(self, group_id: str, target_task_id: int, recipient: str, total_reviews: int) -> None:
        now = time.time()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO review_groups(
                group_id, target_task_id, recipient, total_reviews, summary_sent, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, COALESCE((SELECT summary_sent FROM review_groups WHERE group_id = ?), 0), ?, ?)
            """,
            (group_id, target_task_id, recipient, total_reviews, group_id, now, now),
        )
        conn.commit()
        conn.close()

    def review_group(self, group_id: str) -> sqlite3.Row | None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM review_groups WHERE group_id = ?", (group_id,))
        row = cur.fetchone()
        conn.close()
        return row

    def mark_review_group_sent(self, group_id: str) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE review_groups SET summary_sent = 1, updated_at = ? WHERE group_id = ?",
            (time.time(), group_id),
        )
        conn.commit()
        conn.close()

    def review_tasks(self, group_id: str) -> list[sqlite3.Row]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, model, review_role, status, output_excerpt, error
            FROM tasks
            WHERE review_group_id = ?
            ORDER BY id ASC
            """,
            (group_id,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def update_task_status(self, task_id: int | None, status: str, error: str | None = None) -> None:
        if not task_id:
            return
        now = time.time()
        fields = {
            "queued": ("updated_at = ?", [now]),
            "running": ("status = ?, updated_at = ?, started_at = COALESCE(started_at, ?)", [status, now, now]),
            "done": ("status = ?, updated_at = ?, finished_at = ?, error = NULL", [status, now, now]),
            "timeout": ("status = ?, updated_at = ?, finished_at = ?, error = ?", [status, now, now, error or "timeout"]),
            "failed": ("status = ?, updated_at = ?, finished_at = ?, error = ?", [status, now, now, error or "failed"]),
            "cancelled": ("status = ?, updated_at = ?, finished_at = ?, error = ?", [status, now, now, error or "cancelled"]),
            "waiting_confirm": ("status = ?, updated_at = ?", [status, now]),
        }
        set_clause, values = fields.get(status, ("status = ?, updated_at = ?, error = ?", [status, now, error]))
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", (*values, task_id))
        conn.commit()
        conn.close()

    def recent_tasks(self, limit: int = 5) -> list[sqlite3.Row]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, model, task_kind, content, output_excerpt, status, created_at, updated_at
            FROM tasks
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def task_counts(self) -> dict[str, int]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM tasks
            GROUP BY status
            """
        )
        counts = {row["status"]: int(row["count"]) for row in cur.fetchall()}
        conn.close()
        return counts

    def tasks_by_status(self, statuses: list[str], limit: int = 10) -> list[sqlite3.Row]:
        if not statuses:
            return []
        conn = self._connect()
        cur = conn.cursor()
        placeholders = ", ".join(["?"] * len(statuses))
        cur.execute(
            f"""
            SELECT id, model, task_kind, content, output_excerpt, status, created_at, updated_at, started_at, finished_at, error
            FROM tasks
            WHERE status IN ({placeholders})
            ORDER BY
                CASE status
                    WHEN 'running' THEN 0
                    WHEN 'queued' THEN 1
                    WHEN 'waiting_confirm' THEN 2
                    ELSE 3
                END,
                id ASC
            LIMIT ?
            """,
            (*statuses, limit),
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def latest_task(self, statuses: list[str] | None = None) -> sqlite3.Row | None:
        conn = self._connect()
        cur = conn.cursor()
        if statuses:
            placeholders = ", ".join(["?"] * len(statuses))
            cur.execute(
                f"""
                SELECT id, model, task_kind, content, output_excerpt, status, created_at, updated_at, started_at, finished_at, error
                FROM tasks
                WHERE status IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                tuple(statuses),
            )
        else:
            cur.execute(
                """
                SELECT id, model, task_kind, content, output_excerpt, status, created_at, updated_at, started_at, finished_at, error
                FROM tasks
                ORDER BY id DESC
                LIMIT 1
                """
            )
        row = cur.fetchone()
        conn.close()
        return row

    def latest_completed_task(self, exclude_kinds: tuple[str, ...] = ("review",)) -> sqlite3.Row | None:
        conn = self._connect()
        cur = conn.cursor()
        placeholders = ", ".join(["?"] * len(exclude_kinds))
        cur.execute(
            f"""
            SELECT id, model, task_kind, content, output_excerpt, status, created_at, finished_at
            FROM tasks
            WHERE status = 'done'
              AND task_kind NOT IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            exclude_kinds,
        )
        row = cur.fetchone()
        conn.close()
        return row

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, message_rowid, recipient, model, task_kind, content, attachment,
                   force_search, disable_search, restore_model, output_excerpt, status,
                   created_at, updated_at, started_at, finished_at, error,
                   review_group_id, review_target_task_id, review_role
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        )
        row = cur.fetchone()
        conn.close()
        return row

    def set_pending_confirmation(self, channel_id: str, task: TaskRequest) -> None:
        now = time.time()
        payload = json.dumps(task.__dict__, ensure_ascii=False)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pending_confirmations(channel_id, payload, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (channel_id, payload, now),
        )
        conn.commit()
        conn.close()

    def get_pending_confirmation(self, channel_id: str) -> TaskRequest | None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT payload FROM pending_confirmations WHERE channel_id = ?", (channel_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return TaskRequest(**json.loads(row["payload"]))

    def clear_pending_confirmation(self, channel_id: str) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM pending_confirmations WHERE channel_id = ?", (channel_id,))
        conn.commit()
        conn.close()
