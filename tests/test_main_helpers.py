import asyncio

import main
from state import TaskRequest
from store import BridgeStore


class DummyLogger:
    def warning(self, *args, **kwargs):
        pass


def test_rebuild_queue_removes_target_task():
    original_queue = main.task_queue
    try:
        main.task_queue = asyncio.Queue()
        task1 = TaskRequest(model="claude", content="a", recipient="me", task_id=1)
        task2 = TaskRequest(model="codex", content="b", recipient="me", task_id=2)
        asyncio.run(main.task_queue.put(task1))
        asyncio.run(main.task_queue.put(task2))

        kept, removed = asyncio.run(main.rebuild_queue(excluding_task_id=2))

        assert [task.task_id for task in kept] == [1]
        assert [task.task_id for task in removed] == [2]
        assert main.task_queue.qsize() == 1
    finally:
        main.task_queue = original_queue


def test_task_request_from_row(tmp_path):
    store = BridgeStore(str(tmp_path / "bridge.sqlite"), DummyLogger())
    task = TaskRequest(model="codex", content="hello", recipient="me", task_kind="task")
    task.task_id = store.create_task(task, status="queued")
    row = store.get_task(task.task_id)

    rebuilt = main.task_request_from_row(row)

    assert rebuilt.model == "codex"
    assert rebuilt.content == "hello"
    assert rebuilt.recipient == "me"


def test_maybe_send_review_summary_sends_aggregated_message(tmp_path):
    original_store = main.store
    original_send = main.send_imessage
    sent = []
    try:
        store = BridgeStore(str(tmp_path / "bridge.sqlite"), DummyLogger())
        main.store = store

        async def fake_send(message, recipient, logger):
            sent.append((message, recipient))

        main.send_imessage = fake_send

        store.create_review_group("g1", 42, "me", total_reviews=2)
        task1 = TaskRequest(model="claude", content="review", recipient="me", task_kind="review", review_group_id="g1", review_role="claude")
        task2 = TaskRequest(model="gemini", content="review", recipient="me", task_kind="review", review_group_id="g1", review_role="gemini")
        task1.task_id = store.create_task(task1, status="queued")
        task2.task_id = store.create_task(task2, status="queued")
        store.update_task_result(task1.task_id, "Claude says issue A")
        store.update_task_status(task1.task_id, "done")
        store.update_task_result(task2.task_id, "Gemini says improve B")
        store.update_task_status(task2.task_id, "done")

        asyncio.run(main.maybe_send_review_summary(task1))

        assert len(sent) == 1
        assert "【REVIEW】任务 #42" in sent[0][0]
        assert "Claude 视角" in sent[0][0]
        assert "Gemini 视角" in sent[0][0]
    finally:
        main.store = original_store
        main.send_imessage = original_send
