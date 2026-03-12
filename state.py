import json
import os
import time
from dataclasses import dataclass, field


@dataclass
class TaskRequest:
    model: str
    content: str
    recipient: str
    attachment: str | None = None
    restore_model: str | None = None
    force_search: bool = False
    disable_search: bool = False
    rowid: int | None = None
    task_id: int | None = None
    task_kind: str = "task"
    review_group_id: str | None = None
    review_target_task_id: int | None = None
    review_role: str | None = None
    received_at: float = field(default_factory=time.time)


class ConversationMemory:
    def __init__(self, memory_dir: str, max_turns: int, logger):
        self.memory_dir = memory_dir
        self.max_turns = max_turns
        self.logger = logger
        self._history: dict[str, list[dict]] = {}
        self._has_session: dict[str, bool] = {}
        os.makedirs(self.memory_dir, exist_ok=True)
        self._load_all()

    def _path(self, model: str) -> str:
        return os.path.join(self.memory_dir, f"{model}.json")

    def _load_all(self) -> None:
        for model in ("claude", "gemini", "codex"):
            path = self._path(model)
            if not os.path.exists(path):
                self._history[model] = []
                self._has_session[model] = False
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self._history[model] = data.get("history", [])
                self._has_session[model] = data.get("has_session", False)
                self.logger.info(f"📚 [{model}] 加载历史 {len(self._history[model])} 条")
            except Exception as exc:
                self.logger.warning(f"加载 {model} 历史失败: {exc}")
                self._history[model] = []
                self._has_session[model] = False

    def _save(self, model: str) -> None:
        try:
            with open(self._path(model), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "history": self._history.get(model, []),
                        "has_session": self._has_session.get(model, False),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            self.logger.warning(f"保存 {model} 历史失败: {exc}")

    def add(self, model: str, role: str, content: str) -> None:
        self._history.setdefault(model, [])
        self._history[model].append({"role": role, "content": content, "ts": time.time()})
        if len(self._history[model]) > self.max_turns * 2:
            self._history[model] = self._history[model][-(self.max_turns * 2):]
        if role == "assistant":
            self._has_session[model] = True
        self._save(model)

    def get_context(self, model: str, max_turns: int | None = None) -> str:
        history = self._history.get(model, [])
        if not history:
            return ""
        if max_turns:
            history = history[-(max_turns * 2):]
        lines = ["[对话历史]"]
        for msg in history:
            prefix = "用户" if msg["role"] == "user" else "AI"
            lines.append(f"{prefix}: {msg['content']}")
        lines.append("[以上是历史对话，请继续]\n")
        return "\n".join(lines)

    def has_session(self, model: str) -> bool:
        return self._has_session.get(model, False)

    def reset(self, model: str) -> None:
        self._history[model] = []
        self._has_session[model] = False
        self._save(model)
        self.logger.info(f"🗑️ [{model}] 对话历史已清空")

    def reset_all(self) -> None:
        for model in list(self._history.keys()):
            self.reset(model)

    def summary(self, model: str) -> str:
        history = self._history.get(model, [])
        turns = len([m for m in history if m["role"] == "user"])
        if not history:
            return "无历史记录"
        age_min = int((time.time() - history[-1]["ts"]) / 60)
        return f"{turns} 轮对话，最后活跃 {age_min} 分钟前"


class ModelHealth:
    def __init__(self, state_path: str, logger):
        self.state_path = state_path
        self.logger = logger
        self._success: dict[str, int] = {m: 0 for m in ("claude", "gemini", "codex")}
        self._failure: dict[str, int] = {m: 0 for m in ("claude", "gemini", "codex")}
        self._last_error: dict[str, str] = {}
        self._disabled_until: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            self._success.update(data.get("success", {}))
            self._failure.update(data.get("failure", {}))
            self._last_error.update(data.get("last_error", {}))
            self._disabled_until.update(data.get("disabled_until", {}))
        except Exception as exc:
            self.logger.warning(f"加载模型健康状态失败: {exc}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "success": self._success,
                        "failure": self._failure,
                        "last_error": self._last_error,
                        "disabled_until": self._disabled_until,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            self.logger.warning(f"保存模型健康状态失败: {exc}")

    def is_available(self, model: str) -> bool:
        until = self._disabled_until.get(model, 0)
        if time.time() < until:
            return False
        if model in self._disabled_until and until:
            del self._disabled_until[model]
            self._save()
        return True

    def record_success(self, model: str) -> None:
        self._success[model] = self._success.get(model, 0) + 1
        self._last_error.pop(model, None)
        self._save()

    def record_failure(self, model: str, reason: str, quota: bool = False) -> None:
        self._failure[model] = self._failure.get(model, 0) + 1
        self._last_error[model] = reason[:80]
        if quota:
            self._disabled_until[model] = time.time() + 3600
        elif reason == "auth required":
            self._disabled_until[model] = time.time() + 12 * 3600
        self._save()

    def success_rate(self, model: str) -> str:
        success = self._success.get(model, 0)
        failure = self._failure.get(model, 0)
        total = success + failure
        if total == 0:
            return "无数据"
        return f"{int(success / total * 100)}%"

    def status_line(self, model: str) -> str:
        if not self.is_available(model):
            remain = int((self._disabled_until.get(model, 0) - time.time()) / 60)
            last = self._last_error.get(model, "")
            if last == "auth required":
                return f"⛔ 需重新登录（{remain}分钟后重试）"
            return f"⛔ 配额耗尽（{remain}分钟后恢复）"
        failure = self._failure.get(model, 0)
        rate = self.success_rate(model)
        last = self._last_error.get(model)
        if failure >= 3 and last:
            return f"⚠️ 不稳定 | 成功率 {rate} | 最近: {last}"
        return f"✅ 正常 | 成功率 {rate}"


class AppState:
    def __init__(self, default_model: str):
        self.is_running = False
        self.current_process = None
        self.current_task: TaskRequest | None = None
        self.current_timeout = 0
        self.last_output_at = 0.0
        self.last_message_date = 0
        self.last_message_rowid = 0
        self.task_start_time = 0.0
        self.start_time = time.time()
        self.selected_model = default_model
        self.pending_confirmation: TaskRequest | None = None
        self.db_error_count = 0

    def set_last_seen(self, date_value: int, rowid: int) -> None:
        self.last_message_date = date_value
        self.last_message_rowid = rowid

    def set_running(self, task: TaskRequest, process, timeout: int) -> None:
        self.is_running = True
        self.current_task = task
        self.current_process = process
        self.current_timeout = timeout
        self.task_start_time = time.time()
        self.last_output_at = self.task_start_time

    def clear_running(self) -> None:
        self.is_running = False
        self.current_task = None
        self.current_process = None
        self.current_timeout = 0
        self.last_output_at = 0.0

    def pending_summary(self) -> str:
        if not self.pending_confirmation:
            return "无待确认任务"
        task = self.pending_confirmation
        snippet = task.content[:60].replace("\n", " ")
        return f"{task.model.upper()}: {snippet}"
