import json

from transport import (
    build_osascript_command,
    clear_undelivered_messages,
    load_undelivered_messages,
    persist_undelivered_message,
)


def test_build_osascript_command_keeps_message_as_argv():
    command = build_osascript_command('他说 "hello"\n第二行', "fmon@me.com")

    assert command[:2] == ["osascript", "-e"]
    assert "item 1 of argv" in command[2]
    assert command[3] == '他说 "hello"\n第二行'
    assert command[4] == "fmon@me.com"


def test_persist_undelivered_message_writes_jsonl(tmp_path, monkeypatch):
    target = tmp_path / "undelivered.jsonl"
    monkeypatch.setattr("transport.UNDELIVERED_LOG_PATH", str(target))

    persist_undelivered_message("msg", "me", "timeout")

    row = json.loads(target.read_text(encoding="utf-8").strip())
    assert row["recipient"] == "me"
    assert row["reason"] == "timeout"
    assert row["message"] == "msg"


def test_load_and_clear_undelivered_messages(tmp_path, monkeypatch):
    target = tmp_path / "undelivered.jsonl"
    monkeypatch.setattr("transport.UNDELIVERED_LOG_PATH", str(target))
    persist_undelivered_message("a", "me", "timeout")
    persist_undelivered_message("b", "you", "auth")

    rows = load_undelivered_messages(limit=10)

    assert len(rows) == 2
    assert rows[-1]["message"] == "b"
    assert clear_undelivered_messages() == 2
    assert load_undelivered_messages(limit=10) == []
