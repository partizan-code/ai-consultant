"""Panel queries and local administrative operations; no integration calls."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ai_consultant_bot.config import Settings
from ai_consultant_bot.storage import (
    conversation_chat_ids,
    get_conversation_lead,
    get_setting,
    invalidate_conversation,
    open_database,
    queue_unanswered_message,
    set_setting,
)

CONVERSATIONS = """
WITH members AS (
    SELECT *, MIN(id) OVER (PARTITION BY account_id, lead_id, integration_id) AS conversation_id
    FROM chats
)
"""


def read_settings(settings: Settings) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        return {
            "model": get_setting(connection, "model"),
        }


def update_settings(settings: Settings, model: str) -> dict[str, Any]:
    model = model.strip()
    if not model or len(model) > settings.model_max_chars or any(c.isspace() for c in model):
        raise ValueError("Invalid model identifier")
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            set_setting(connection, "model", model)
    return {"model": model}


def list_chats(settings: Settings, offset: int = 0) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            CONVERSATIONS + "SELECT c.*, (SELECT p.instagram_username FROM chat_profiles p "
            "JOIN members g ON g.id = p.chat_id WHERE g.conversation_id = c.id "
            "ORDER BY julianday(p.occurred_at) DESC, p.chat_id DESC LIMIT 1) AS instagram_username, "
            "(SELECT text FROM messages m JOIN members g ON g.id = m.chat_id "
            "WHERE g.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_text, "
            "(SELECT GROUP_CONCAT(DISTINCT source_type) FROM members g "
            "WHERE g.conversation_id = c.id) AS source_types, "
            "(SELECT MAX(paused) FROM members g WHERE g.conversation_id = c.id) "
            "AS conversation_paused FROM members c WHERE c.id = c.conversation_id "
            "ORDER BY (SELECT MAX(m.id) FROM messages m JOIN members g ON g.id = m.chat_id "
            "WHERE g.conversation_id = c.id) DESC, c.id DESC "
            "LIMIT ? OFFSET ?",
            (settings.panel_page_size, offset),
        ).fetchall()
        return {
            "items": [
                {
                    **dict(row),
                    **asdict(get_conversation_lead(connection, row["id"])),
                    "paused": row["conversation_paused"],
                }
                for row in rows
            ],
            "total": connection.execute(
                CONVERSATIONS + "SELECT COUNT(DISTINCT conversation_id) FROM members"
            ).fetchone()[0],
        }


def chat_detail(settings: Settings, chat_id: int, offset: int = 0) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        # Keep the snapshot consistent while the worker/webhooks write on other connections.
        connection.execute("BEGIN")
        member_ids = conversation_chat_ids(connection, chat_id)
        placeholders = ",".join("?" for _ in member_ids)
        chat = connection.execute("SELECT * FROM chats WHERE id = ?", (member_ids[0],)).fetchone()
        chat = dict(chat)
        members = connection.execute(
            f"SELECT id, source_type, paused FROM chats WHERE id IN ({placeholders})", member_ids
        ).fetchall()
        chat["paused"] = max(row["paused"] for row in members)
        profile = connection.execute(
            f"SELECT instagram_username FROM chat_profiles WHERE chat_id IN ({placeholders}) "
            "ORDER BY julianday(occurred_at) DESC, chat_id DESC LIMIT 1",
            member_ids,
        ).fetchone()
        chat["instagram_username"] = profile[0] if profile else None
        direct_ids = [row["id"] for row in members if row["source_type"] == "message"]
        reply_block_reason = (
            None
            if len(direct_ids) == 1
            else "ambiguous_direct_route"
            if direct_ids
            else "comment_private_reply_unverified"
        )
        messages = connection.execute(
            "SELECT m.*, ctx.post_url, ctx.post_description, ctx.post_owner, "
            "ctx.reply_block_reason, p.photo_url AS post_photo_url "
            "FROM messages m LEFT JOIN message_context ctx "
            "ON ctx.message_id = m.id LEFT JOIN message_previews p ON p.message_id = m.id "
            f"WHERE m.chat_id IN ({placeholders}) "
            "ORDER BY julianday(m.occurred_at) DESC, m.id DESC LIMIT ? OFFSET ?",
            (*member_ids, settings.panel_page_size, offset),
        ).fetchall()
        suggestion = connection.execute(
            "SELECT * FROM suggestions WHERE conversation_id = ?", (member_ids[0],)
        ).fetchone()
        attempts = connection.execute(
            "SELECT a.*, EXISTS (SELECT 1 FROM outgoing_receipts r JOIN messages m "
            "ON m.id = r.message_id WHERE r.custom_id = a.request_id "
            "AND m.chat_id = a.chat_id AND m.text = a.text) AS confirmed "
            f"FROM outgoing_attempts a WHERE a.chat_id IN ({placeholders}) "
            "ORDER BY a.created_at DESC LIMIT ?",
            (*member_ids, settings.panel_page_size),
        ).fetchall()
        return {
            "chat": chat,
            "reply_chat_id": direct_ids[0] if len(direct_ids) == 1 else None,
            "lead": asdict(get_conversation_lead(connection, chat_id)),
            "messages": [dict(row) for row in reversed(messages)],
            "message_total": connection.execute(
                f"SELECT COUNT(*) FROM messages WHERE chat_id IN ({placeholders})", member_ids
            ).fetchone()[0],
            "suggestion": dict(suggestion) if suggestion else None,
            "attempts": [dict(row) for row in attempts],
            "reply_block_reason": reply_block_reason,
        }


def set_pause(settings: Settings, chat_id: int, paused: bool) -> dict[str, bool]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            member_ids = conversation_chat_ids(connection, chat_id)
            previous = connection.execute(
                "SELECT MAX(paused) FROM chats WHERE id IN ("
                + ",".join("?" for _ in member_ids)
                + ")",
                member_ids,
            ).fetchone()[0]
            if bool(previous) == paused:
                return {"paused": paused}
            invalidate_conversation(connection, chat_id)
            for member_id in member_ids:
                connection.execute(
                    "UPDATE chats SET paused = ? WHERE id = ?", (int(paused), member_id)
                )
            if paused:
                queue_unanswered_message(connection, chat_id)
    return {"paused": paused}


def service_status(settings: Settings) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        rows = connection.execute(
            "SELECT * FROM api_calls ORDER BY id DESC LIMIT ?", (settings.panel_page_size,)
        ).fetchall()
        errors = connection.execute(
            "SELECT * FROM api_calls WHERE error_code IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        jobs = connection.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()
        return {
            "calls": [dict(row) for row in rows],
            "last_error": dict(errors) if errors else None,
            "jobs": {row["status"]: row["count"] for row in jobs},
            "worker_enabled": bool(settings.openrouter_api_key) and settings.external_apis_enabled,
        }
