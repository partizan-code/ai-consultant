"""Explicit legacy migration on disposable SQLite files only."""

import sqlite3
from pathlib import Path

import pytest

from ai_consultant_bot.config import Settings
from ai_consultant_bot.migrate_suggestions import migrate
from ai_consultant_bot.storage import SCHEMA, initialize_database, open_database


def legacy_database(path: Path, enabled: str) -> None:
    schema = SCHEMA.replace("    generation INTEGER NOT NULL DEFAULT 0,\n", "")
    schema = schema.replace("    message_id INTEGER REFERENCES messages(id),\n", "")
    start = schema.index("CREATE TABLE IF NOT EXISTS suggestions")
    end = schema.index(";", start) + 1
    schema = (
        schema[:start]
        + """
CREATE TABLE drafts (
    id INTEGER PRIMARY KEY, chat_id INTEGER REFERENCES chats(id),
    message_id INTEGER REFERENCES messages(id), text TEXT, status TEXT DEFAULT 'pending'
);"""
        + schema[end:]
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.executemany(
            "INSERT INTO settings VALUES (?, ?)",
            [("model", "chosen/model"), ("auto_reply", enabled)],
        )
        connection.execute(
            "INSERT INTO chats(id, account_id, lead_id, integration_id, source_type, source_id, paused) "
            "VALUES (1, 'a', 'l', 'i', 'message', 's', 0)"
        )
        connection.execute(
            "INSERT INTO chats(id, account_id, lead_id, integration_id, source_type, source_id, paused) "
            "VALUES (2, 'a', 'other', 'i', 'message', 's', 1)"
        )
        connection.execute(
            "INSERT INTO messages(id, chat_id, external_id, direction, text, occurred_at) "
            "VALUES (1, 1, 'in', 'incoming', 'Нужен диван', '2026-11-20T10:00:00Z')"
        )
        connection.execute("INSERT INTO leads(chat_id, name, city) VALUES (1, 'Анна', 'Минск')")
        connection.execute("INSERT INTO jobs(message_id, status) VALUES (1, 'unknown')")
        connection.execute("INSERT INTO drafts(chat_id, message_id, text) VALUES (1, 1, 'Старый')")
        connection.execute(
            "INSERT INTO outgoing_attempts(request_id, chat_id, text, status, error_code) "
            "VALUES ('consultation:1', 1, 'Ответ', 'unknown', 'timeout')"
        )


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_migration_removes_legacy_and_preserves_business_data(tmp_path: Path, enabled: str) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path, enabled)
    with open_database(path, timeout=1) as connection:
        tables = ("messages", "leads", "outgoing_attempts", "jobs")
        before = {
            name: [dict(row) for row in connection.execute(f"SELECT * FROM {name}")]
            for name in tables
        }
        with pytest.raises(RuntimeError, match="migrate_suggestions"):
            initialize_database(connection, Settings())
        migrate(connection)
        migrate(connection)  # Explicit reruns are safe and do not change activity.
        initialize_database(connection, Settings())
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'drafts'"
        ).fetchone()
        assert not connection.execute("SELECT 1 FROM settings WHERE key = 'auto_reply'").fetchone()
        assert (
            connection.execute("SELECT value FROM settings WHERE key = 'model'").fetchone()[0]
            == "chosen/model"
        )
        assert [row[0] for row in connection.execute("SELECT paused FROM chats ORDER BY id")] == [
            int(enabled == "0"),
            1,
        ]
        for name, rows in before.items():
            after = [dict(row) for row in connection.execute(f"SELECT * FROM {name}")]
            assert [{key: row[key] for key in rows[0]} for row in after] == rows
        assert connection.execute("SELECT message_id FROM outgoing_attempts").fetchone()[0] == 1
        assert not connection.execute("SELECT * FROM suggestions").fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None


def test_migration_rolls_back_schema_and_settings_on_integrity_failure(tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite3"
    legacy_database(path, "0")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE messages SET chat_id = 999")
    with open_database(path, timeout=1) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="rolled back"):
            migrate(connection)
        assert connection.execute("SELECT 1 FROM drafts").fetchone()
        assert (
            connection.execute("SELECT value FROM settings WHERE key = 'auto_reply'").fetchone()[0]
            == "0"
        )
        assert "generation" not in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert connection.execute("SELECT paused FROM chats WHERE id = 1").fetchone()[0] == 0
