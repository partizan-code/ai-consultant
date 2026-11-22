"""Explicit offline migration; never called by backend startup."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def migrate(connection: sqlite3.Connection) -> None:
    """Run with the backend stopped and a backup made; all changes are atomic."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        legacy = connection.execute(
            "SELECT value FROM settings WHERE key = 'auto_reply'"
        ).fetchone()
        if legacy is not None and legacy[0] != "1":
            connection.execute("UPDATE chats SET paused = 1")
        connection.execute("DROP TABLE IF EXISTS drafts")
        connection.execute("DELETE FROM settings WHERE key = 'auto_reply'")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "generation" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outgoing_attempts)")}
        if "message_id" not in columns:
            connection.execute(
                "ALTER TABLE outgoing_attempts ADD COLUMN message_id INTEGER REFERENCES messages(id)"
            )
            connection.execute(
                "UPDATE outgoing_attempts SET message_id = (SELECT MAX(m.id) FROM messages m "
                "JOIN chats c ON c.id = m.chat_id JOIN chats target "
                "ON target.id = outgoing_attempts.chat_id WHERE c.account_id = target.account_id "
                "AND c.lead_id = target.lead_id AND c.integration_id = target.integration_id "
                "AND m.direction = 'incoming' AND m.imported = 0 "
                "AND julianday(m.created_at) <= julianday(outgoing_attempts.created_at))"
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Foreign key check failed; migration rolled back")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database", type=Path, help="Existing SQLite file; stop backend and back up first"
    )
    args = parser.parse_args()
    # mode=rw prevents accidentally creating a new database at a misspelled path.
    connection = sqlite3.connect(args.database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        migrate(connection)
    finally:
        connection.close()
    print("Migration completed.")


if __name__ == "__main__":
    main()
