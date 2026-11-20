"""SQLite schema and persistence operations; no network access."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_consultant_bot.config import Settings

if TYPE_CHECKING:
    from ai_consultant_bot.events import MessageEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (account_id, lead_id, integration_id, source_type, source_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    external_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    text TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (chat_id, external_id)
);
CREATE TABLE IF NOT EXISTS leads (
    chat_id INTEGER PRIMARY KEY REFERENCES chats(id),
    name TEXT,
    phone TEXT,
    furniture_type TEXT,
    dimensions TEXT,
    budget TEXT,
    city TEXT,
    purchase_time TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'stale', 'sent', 'failed', 'unknown')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed', 'unknown')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    operation TEXT NOT NULL,
    status_code INTEGER,
    elapsed_ms INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS messages_external_id ON messages(external_id);
CREATE TABLE IF NOT EXISTS message_context (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    source_real_id TEXT,
    sender_id TEXT,
    post_url TEXT,
    post_description TEXT,
    post_owner TEXT,
    reply_block_reason TEXT
);
CREATE TABLE IF NOT EXISTS outgoing_attempts (
    request_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unknown', 'sent', 'failed', 'blocked')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_uncertain_send_per_chat
    ON outgoing_attempts(chat_id) WHERE status = 'unknown';
CREATE TABLE IF NOT EXISTS outgoing_receipts (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    custom_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outgoing_receipt_custom_id ON outgoing_receipts(custom_id);
"""


@dataclass(frozen=True)
class ChatKey:
    account_id: str
    lead_id: str
    integration_id: str
    source_type: str
    source_id: str


@dataclass(frozen=True)
class Lead:
    name: str | None = None
    phone: str | None = None
    furniture_type: str | None = None
    dimensions: str | None = None
    budget: str | None = None
    city: str | None = None
    purchase_time: str | None = None


@contextmanager
def open_database(path: Path, *, timeout: float) -> Generator[sqlite3.Connection]:
    """Create the parent if needed and close the connection even on failure.

    Callers use `with connection:` to group writes into an atomic transaction.
    Use one connection per processing unit/thread rather than sharing connections.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def initialize_database(connection: sqlite3.Connection, settings: Settings) -> None:
    """Initialize a new database idempotently without overwriting saved settings."""
    connection.executescript(SCHEMA)
    with connection:
        connection.executemany(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            [("model", settings.default_model), ("auto_reply", str(int(settings.auto_reply)))],
        )


def get_setting(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return row["value"]


def set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    if key not in {"model", "auto_reply"}:
        raise ValueError("Unsupported persistent setting")
    if not value.strip() or (key == "auto_reply" and value not in {"0", "1"}):
        raise ValueError("Invalid persistent setting value")
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_chat(connection: sqlite3.Connection, key: ChatKey) -> int:
    values = tuple(asdict(key).values())
    connection.execute(
        "INSERT INTO chats(account_id, lead_id, integration_id, source_type, source_id) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        values,
    )
    row = connection.execute(
        "SELECT id FROM chats WHERE account_id = ? AND lead_id = ? AND integration_id = ? "
        "AND source_type = ? AND source_id = ?",
        values,
    ).fetchone()
    return row["id"]


def save_message(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    external_id: str,
    direction: str,
    text: str,
    occurred_at: str,
    imported: bool = False,
) -> bool:
    """Return true only for a new message; replay never overwrites stored history.

    Queueing and source metadata are handled by store_event.
    """
    cursor = connection.execute(
        "INSERT INTO messages(chat_id, external_id, direction, text, occurred_at, imported) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, external_id) DO NOTHING",
        (chat_id, external_id, direction, text, occurred_at, int(imported)),
    )
    return cursor.rowcount == 1


def save_lead(connection: sqlite3.Connection, chat_id: int, lead: Lead) -> None:
    """Merge known fields, preserving existing data when a partial update omits it."""
    connection.execute(
        "INSERT INTO leads(chat_id, name, phone, furniture_type, dimensions, budget, city, "
        "purchase_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
        "name = COALESCE(excluded.name, leads.name), "
        "phone = COALESCE(excluded.phone, leads.phone), "
        "furniture_type = COALESCE(excluded.furniture_type, leads.furniture_type), "
        "dimensions = COALESCE(excluded.dimensions, leads.dimensions), "
        "budget = COALESCE(excluded.budget, leads.budget), "
        "city = COALESCE(excluded.city, leads.city), "
        "purchase_time = COALESCE(excluded.purchase_time, leads.purchase_time), "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
        (chat_id, *asdict(lead).values()),
    )


def store_event(
    connection: sqlite3.Connection, event: MessageEvent, *, imported: bool = False
) -> bool:
    """Caller holds BEGIN IMMEDIATE; deduplicate across lead/source changes.

    Umnico message IDs are scoped to account and integration, never webhookId.
    Replayed imported messages stay imported and never acquire a reply job.
    """
    existing = connection.execute(
        "SELECT m.id, m.direction FROM messages m JOIN chats c ON c.id = m.chat_id "
        "WHERE c.account_id = ? AND c.integration_id = ? AND m.external_id = ?",
        (event.chat.account_id, event.chat.integration_id, event.external_id),
    ).fetchone()
    if existing is not None:
        if existing["direction"] != event.direction:
            raise ValueError("Conflicting message direction")
        save_outgoing_receipt(connection, existing["id"], event)
        return False
    chat_id = save_chat(connection, event.chat)
    save_message(
        connection,
        chat_id=chat_id,
        external_id=event.external_id,
        direction=event.direction,
        text=event.text,
        occurred_at=event.occurred_at,
        imported=imported,
    )
    message_id = connection.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND external_id = ?",
        (chat_id, event.external_id),
    ).fetchone()["id"]
    connection.execute(
        "INSERT INTO message_context(message_id, source_real_id, sender_id, post_url, "
        "post_description, post_owner, reply_block_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            event.source_real_id,
            event.sender_id,
            event.post_url,
            event.post_description,
            event.post_owner,
            "comment_private_reply_unverified" if event.chat.source_type == "comment" else None,
        ),
    )
    save_outgoing_receipt(connection, message_id, event)
    if not imported and event.direction == "incoming":
        connection.execute(
            "UPDATE drafts SET status = 'stale' WHERE chat_id = ? "
            "AND status IN ('pending', 'approved')",
            (chat_id,),
        )
        if event.text.strip():
            connection.execute("INSERT INTO jobs(message_id) VALUES (?)", (message_id,))
    return True


def reconcile_outgoing_attempt(connection: sqlite3.Connection, request_id: str) -> None:
    """A matching outgoing receipt resolves uncertainty, never authorizes a resend."""
    connection.execute(
        "UPDATE outgoing_attempts SET status = 'sent', error_code = NULL "
        "WHERE request_id = ? AND status IN ('unknown', 'failed') AND EXISTS ("
        "SELECT 1 FROM outgoing_receipts r JOIN messages m ON m.id = r.message_id "
        "WHERE r.custom_id = outgoing_attempts.request_id "
        "AND m.chat_id = outgoing_attempts.chat_id AND m.text = outgoing_attempts.text "
        "AND m.direction = 'outgoing')",
        (request_id,),
    )
    confirmed = connection.execute(
        "SELECT 1 FROM outgoing_attempts WHERE request_id = ? AND status = 'sent'",
        (request_id,),
    ).fetchone()
    if confirmed:
        connection.execute(
            "UPDATE drafts SET status = 'sent' WHERE 'draft:' || id = ? "
            "AND status IN ('unknown', 'failed')",
            (request_id,),
        )
        connection.execute(
            "UPDATE drafts SET status = 'sent' WHERE status IN ('pending', 'approved', 'unknown') "
            "AND message_id IN (SELECT message_id FROM jobs WHERE 'consultation:' || id = ?)",
            (request_id,),
        )
        connection.execute(
            "UPDATE jobs SET status = 'done' WHERE 'consultation:' || id = ? "
            "AND status IN ('processing', 'unknown')",
            (request_id,),
        )


def save_outgoing_receipt(
    connection: sqlite3.Connection, message_id: int, event: MessageEvent
) -> None:
    if event.direction != "outgoing" or event.custom_id is None:
        return
    # A duplicate under a different route must not confirm a send in the old chat.
    matches = connection.execute(
        "SELECT 1 FROM messages m JOIN chats c ON c.id = m.chat_id "
        "WHERE m.id = ? AND m.text = ? AND m.direction = 'outgoing' "
        "AND c.account_id = ? AND c.lead_id = ? AND c.integration_id = ? "
        "AND c.source_type = ? AND c.source_id = ?",
        (message_id, event.text, *asdict(event.chat).values()),
    ).fetchone()
    if matches is None:
        return
    previous = connection.execute(
        "SELECT custom_id FROM outgoing_receipts WHERE message_id = ?", (message_id,)
    ).fetchone()
    if previous is not None and previous["custom_id"] != event.custom_id:
        raise ValueError("Conflicting custom ID")
    connection.execute(
        "INSERT INTO outgoing_receipts(message_id, custom_id) VALUES (?, ?) "
        "ON CONFLICT(message_id) DO NOTHING",
        (message_id, event.custom_id),
    )
    reconcile_outgoing_attempt(connection, event.custom_id)


def claim_next_job(connection: sqlite3.Connection) -> sqlite3.Row | None:
    """Caller holds BEGIN IMMEDIATE; one active job per unpaused chat, FIFO."""
    row = connection.execute(
        "SELECT j.id, j.message_id, m.chat_id FROM jobs j "
        "JOIN messages m ON m.id = j.message_id JOIN chats c ON c.id = m.chat_id "
        "WHERE j.status = 'pending' AND c.paused = 0 "
        "AND NOT EXISTS (SELECT 1 FROM jobs active JOIN messages am "
        "ON am.id = active.message_id WHERE am.chat_id = c.id "
        "AND active.status IN ('processing', 'unknown')) "
        "AND NOT EXISTS (SELECT 1 FROM outgoing_attempts o "
        "WHERE o.chat_id = c.id AND o.status = 'unknown') ORDER BY j.id LIMIT 1"
    ).fetchone()
    if row is not None:
        connection.execute("UPDATE jobs SET status = 'processing' WHERE id = ?", (row["id"],))
    return row


def record_api_call(
    connection: sqlite3.Connection,
    operation: str,
    elapsed_ms: int,
    *,
    status_code: int | None = None,
    error_code: str | None = None,
    service: str = "umnico",
) -> None:
    connection.execute(
        "INSERT INTO api_calls(service, operation, status_code, elapsed_ms, error_code) "
        "VALUES (?, ?, ?, ?, ?)",
        (service, operation, status_code, elapsed_ms, error_code),
    )


def get_lead(connection: sqlite3.Connection, chat_id: int) -> Lead:
    row = connection.execute("SELECT * FROM leads WHERE chat_id = ?", (chat_id,)).fetchone()
    return Lead(**{name: row[name] for name in asdict(Lead())}) if row else Lead()


def consultation_is_current(connection: sqlite3.Connection, job_id: int) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM jobs j JOIN messages m ON m.id = j.message_id "
            "JOIN chats c ON c.id = m.chat_id WHERE j.id = ? AND j.status = 'processing' "
            "AND c.paused = 0 AND NOT EXISTS (SELECT 1 FROM messages newer "
            "WHERE newer.chat_id = c.id AND newer.direction = 'incoming' "
            "AND newer.imported = 0 AND newer.id > m.id)",
            (job_id,),
        ).fetchone()
        is not None
    )


def load_consultation_context(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    settings: Settings,
) -> dict[str, Any]:
    chat = connection.execute("SELECT * FROM chats WHERE id = ?", (job["chat_id"],)).fetchone()
    rows = connection.execute(
        "SELECT direction, text FROM ("
        "SELECT direction, text, occurred_at, id AS sequence FROM messages WHERE chat_id = ? "
        "UNION ALL SELECT 'outgoing', a.text, a.created_at, 0 FROM outgoing_attempts a "
        "WHERE a.chat_id = ? AND a.status = 'sent' AND NOT EXISTS ("
        "SELECT 1 FROM outgoing_receipts r JOIN messages m ON m.id = r.message_id "
        "WHERE r.custom_id = a.request_id AND m.chat_id = a.chat_id AND m.text = a.text)) "
        "ORDER BY (sequence = ?) DESC, julianday(occurred_at) DESC, sequence DESC LIMIT ?",
        (job["chat_id"], job["chat_id"], job["message_id"], settings.history_context_messages),
    ).fetchall()
    remaining = settings.history_context_chars
    history = []
    for row in rows:
        if remaining <= 0:
            break
        text = row["text"][-remaining:]
        history.append({"direction": row["direction"], "text": text})
        remaining -= len(text)
    post = connection.execute(
        "SELECT post_url, post_description, post_owner FROM message_context WHERE message_id = ?",
        (job["message_id"],),
    ).fetchone()
    return {
        "history": list(reversed(history)),
        "lead": get_lead(connection, job["chat_id"]),
        "source_type": chat["source_type"],
        "post": {key: (post[key] or "")[: settings.knowledge_chunk_chars] for key in post.keys()}
        if post
        else {},
        "model": get_setting(connection, "model"),
    }


def save_consultation(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    lead: Lead,
    reply: str,
) -> int | None:
    """Caller holds BEGIN IMMEDIATE. A stale generation cannot update a newer draft/lead."""
    if not consultation_is_current(connection, job["id"]):
        finish_consultation_job(connection, job["id"], "done")
        return None
    save_lead(connection, job["chat_id"], lead)
    connection.execute(
        "UPDATE drafts SET status = 'stale' WHERE chat_id = ? AND status IN ('pending', 'approved')",
        (job["chat_id"],),
    )
    cursor = connection.execute(
        "INSERT INTO drafts(chat_id, message_id, text) VALUES (?, ?, ?)",
        (job["chat_id"], job["message_id"], reply),
    )
    return cursor.lastrowid


def finish_consultation_job(
    connection: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    draft_id: int | None = None,
    draft_status: str | None = None,
) -> None:
    connection.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    if draft_id is not None and draft_status is not None:
        connection.execute(
            "UPDATE drafts SET status = ? WHERE id = ? AND status != 'stale'",
            (draft_status, draft_id),
        )


def recover_interrupted_jobs(connection: sqlite3.Connection) -> int:
    """Single-backend startup: retry generation only if no send was reserved."""
    rows = connection.execute(
        "SELECT j.id, j.message_id, a.status FROM jobs j LEFT JOIN outgoing_attempts a "
        "ON a.request_id = 'consultation:' || j.id WHERE j.status = 'processing'",
    ).fetchall()
    for row in rows:
        status = row["status"]
        job_status = "pending" if status is None else "unknown" if status == "unknown" else "done"
        finish_consultation_job(connection, row["id"], job_status)
        if status is not None:
            connection.execute(
                "UPDATE drafts SET status = ? WHERE message_id = ? AND status != 'stale'",
                (status if status in {"sent", "unknown"} else "failed", row["message_id"]),
            )
    return len(rows)


def interrupt_consultation(
    connection: sqlite3.Connection,
    job_id: int,
    draft_id: int | None,
) -> None:
    attempt = connection.execute(
        "SELECT status FROM outgoing_attempts WHERE request_id = ?",
        (f"consultation:{job_id}",),
    ).fetchone()
    status = attempt["status"] if attempt else "failed"
    finish_consultation_job(
        connection,
        job_id,
        "done" if status in {"sent", "blocked"} else status,
        draft_id=draft_id,
        draft_status=status if status in {"sent", "unknown"} else "failed",
    )


def cancel_generation(connection: sqlite3.Connection, chat_id: int) -> None:
    """Invalidate an in-flight generation even if the chat is immediately resumed.

    A reserved send cannot be recalled. Pending jobs remain queued for resume.
    """
    connection.execute(
        "UPDATE jobs SET status = 'done' WHERE status = 'processing' "
        "AND message_id IN (SELECT id FROM messages WHERE chat_id = ?) "
        "AND NOT EXISTS (SELECT 1 FROM outgoing_attempts a "
        "WHERE a.request_id = 'consultation:' || jobs.id)",
        (chat_id,),
    )
