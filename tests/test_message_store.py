import sqlite3

from message_store import fetch_new_messages


class DummyLogger:
    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        raise AssertionError(args[0] if args else "unexpected logger error")


def _create_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE handle (rowid INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (
            rowid INTEGER PRIMARY KEY,
            text TEXT,
            attributedBody BLOB,
            date INTEGER,
            handle_id INTEGER,
            is_from_me INTEGER
        );
        CREATE TABLE attachment (
            rowid INTEGER PRIMARY KEY,
            filename TEXT,
            mime_type TEXT
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );
        """
    )
    cur.execute("INSERT INTO handle(rowid, id) VALUES (1, 'me@example.com')")
    cur.execute(
        "INSERT INTO message(rowid, text, attributedBody, date, handle_id, is_from_me) VALUES (1, 'first', NULL, 100, 1, 0)"
    )
    cur.execute(
        "INSERT INTO message(rowid, text, attributedBody, date, handle_id, is_from_me) VALUES (2, 'second', NULL, 101, 1, 0)"
    )
    cur.execute(
        "INSERT INTO message(rowid, text, attributedBody, date, handle_id, is_from_me) VALUES (3, NULL, NULL, 101, 1, 0)"
    )
    cur.execute("INSERT INTO attachment(rowid, filename, mime_type) VALUES (1, '/tmp/demo.png', 'image/png')")
    cur.execute("INSERT INTO message_attachment_join(message_id, attachment_id) VALUES (3, 1)")
    conn.commit()
    conn.close()


def test_fetch_new_messages_returns_all_new_rows_in_order(tmp_path):
    db_path = tmp_path / "chat.db"
    _create_db(db_path)
    logger = DummyLogger()

    messages = fetch_new_messages(str(db_path), ["me@example.com"], 100, 1, logger)

    assert [message.rowid for message in messages] == [2, 3]
    assert messages[1].attachment == "/tmp/demo.png"
