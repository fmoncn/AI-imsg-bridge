import tempfile

from state import TaskRequest
from store import BridgeStore


class DummyLogger:
    def warning(self, *args, **kwargs):
        pass


def test_store_task_roundtrip():
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        store = BridgeStore(tmp.name, DummyLogger())
        task = TaskRequest(model="codex", content="hello", recipient="me", task_kind="task")
        task.task_id = store.create_task(task, status="queued")
        store.update_task_status(task.task_id, "running")
        store.update_task_result(task.task_id, "result text")
        store.update_task_status(task.task_id, "done")

        row = store.get_task(task.task_id)

        assert row is not None
        assert row["model"] == "codex"
        assert row["status"] == "done"
        assert row["output_excerpt"] == "result text"


def test_review_group_roundtrip():
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        store = BridgeStore(tmp.name, DummyLogger())
        store.create_review_group("g1", 12, "me", total_reviews=2)
        group = store.review_group("g1")

        assert group is not None
        assert group["target_task_id"] == 12
        assert group["summary_sent"] == 0


def test_pending_confirmation_roundtrip():
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        store = BridgeStore(tmp.name, DummyLogger())
        task = TaskRequest(model="claude", content="danger", recipient="me", task_kind="task")

        store.set_pending_confirmation("me", task)
        restored = store.get_pending_confirmation("me")

        assert restored is not None
        assert restored.content == "danger"
        assert restored.model == "claude"

        store.clear_pending_confirmation("me")
        assert store.get_pending_confirmation("me") is None
