import asyncio

import main
import transport
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


def test_load_bridge_context_uses_engine_keyword_pattern(tmp_path):
    original_user = main._USER_CONTEXT_PATH
    original_bridge = main._BRIDGE_CONTEXT_PATH
    try:
        user_path = tmp_path / "USER.md"
        bridge_path = tmp_path / "BRIDGE.md"
        user_path.write_text("user ctx", encoding="utf-8")
        bridge_path.write_text("bridge ctx", encoding="utf-8")
        main._USER_CONTEXT_PATH = str(user_path)
        main._BRIDGE_CONTEXT_PATH = str(bridge_path)

        text = main.load_bridge_context("请帮我优化这个 bridge 项目")

        assert "user ctx" in text
        assert "bridge ctx" in text
    finally:
        main._USER_CONTEXT_PATH = original_user
        main._BRIDGE_CONTEXT_PATH = original_bridge

def test_classify_cli_failure_recognizes_auth_and_capacity():
    assert main.classify_cli_failure("Opening authentication page in your browser") == ("auth required", "需要登录/认证")
    assert main.classify_cli_failure("No capacity available for model gemini-3.1-pro-preview") == ("quota exhausted", "模型拥塞/配额受限")
    assert main.classify_cli_failure("normal output") is None


def test_cli_model_lines_show_selected_model(monkeypatch):
    monkeypatch.setattr(main.app_state, "selected_model", "gemini")

    lines = main.cli_model_lines()

    assert lines[0] == "🧠 CLI 模型："
    assert lines[1].startswith("  CLAUDE:")
    assert lines[2].startswith("▶ GEMINI:")
    assert lines[3].startswith("  CODEX:")


def test_status_message_includes_routing_policy():
    uptime_sec = 5
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime = f"{hours}小时{minutes}分{seconds}秒" if hours else f"{minutes}分{seconds}秒"
    counts = main.store.task_counts()
    active_count = counts.get("running", 0) + counts.get("queued", 0) + counts.get("waiting_confirm", 0)
    msg = (
        f"🤖 当前模型: {main.app_state.selected_model.upper()}\n"
        f"🧭 路由策略: 默认对话→GEMINI | 执行任务→CODEX | CLAUDE 仅 /c\n"
        f"{main.current_task_status()}\n"
        f"⏱️ 运行时长: {uptime}\n"
        f"📋 活动任务: {active_count} 条（运行 {counts.get('running', 0)} / 排队 {counts.get('queued', 0)} / 待确认 {counts.get('waiting_confirm', 0)}）\n"
    )

    assert "默认对话→GEMINI" in msg
    assert "执行任务→CODEX" in msg
    assert "CLAUDE 仅 /c" in msg


def test_heartbeat_default_is_disabled():
    assert main.HEARTBEAT_ENABLED is False


def test_pick_available_model_avoids_disabled_model(monkeypatch):
    monkeypatch.setattr(main.health, "_disabled_until", {"claude": main.time.time() + 600})

    assert main.pick_available_model("claude") == "gemini"


def test_build_fallback_task_preserves_request_shape():
    task = TaskRequest(model="gemini", content="hi", recipient="me", attachment="/tmp/a.png", force_search=True, disable_search=False, rowid=12)

    rebuilt = main.build_fallback_task(task, "claude")

    assert rebuilt.model == "claude"
    assert rebuilt.content == "hi"
    assert rebuilt.attachment == "/tmp/a.png"
    assert rebuilt.force_search is True
    assert rebuilt.rowid == 12


def test_handle_fallback_creates_persisted_task(tmp_path):
    original_store = main.store
    original_send = main.send_imessage
    original_run = main.run_ai_task
    original_is_available = main.health.is_available
    sent = []
    rerun = []
    try:
        main.store = BridgeStore(str(tmp_path / "bridge.sqlite"), DummyLogger())

        async def fake_send(message, recipient, logger):
            sent.append(message)

        async def fake_run(task):
            rerun.append(task)

        main.send_imessage = fake_send
        main.run_ai_task = fake_run
        main.health.is_available = lambda model: model != "codex"
        task = TaskRequest(model="codex", content="总结今天 bridge 的进展", recipient="me")
        task.task_id = main.store.create_task(task, status="queued")

        ok = asyncio.run(main.handle_fallback(task, "quota exhausted", "模型拥塞/配额受限"))

        assert ok is True
        assert len(sent) == 1
        assert len(rerun) == 1
        assert rerun[0].model == "gemini"
        assert rerun[0].task_id is not None
        assert main.store.get_task(rerun[0].task_id)["model"] == "gemini"
    finally:
        main.store = original_store
        main.send_imessage = original_send
        main.run_ai_task = original_run
        main.health.is_available = original_is_available


def test_undelivered_snapshot_lines(tmp_path, monkeypatch):
    target = tmp_path / "undelivered.jsonl"
    monkeypatch.setattr("transport.UNDELIVERED_LOG_PATH", str(target))
    transport.persist_undelivered_message("first", "me", "timeout")

    lines = main.undelivered_snapshot_lines()

    assert lines[0] == "📭 未送达消息："
    assert "timeout" in lines[1]


def test_current_task_status_includes_last_output_age():
    task = TaskRequest(model="codex", content="x", recipient="me", task_id=7)
    main.app_state.current_task = task
    main.app_state.is_running = True
    main.app_state.current_timeout = 120
    now = main.time.time()
    main.app_state.task_start_time = now - 10
    main.app_state.last_output_at = now - 3

    line = main.current_task_status()

    assert "最后输出 3s 前" in line

    main.app_state.clear_running()
