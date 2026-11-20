"""Panel queries and local administrative operations; no integration calls."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ai_consultant_bot.config import Settings
from ai_consultant_bot.storage import (
    cancel_generation,
    get_lead,
    get_setting,
    open_database,
    set_setting,
)


def read_settings(settings: Settings) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        return {
            "model": get_setting(connection, "model"),
            "auto_reply": get_setting(connection, "auto_reply") == "1",
        }


def update_settings(settings: Settings, model: str, auto_reply: bool) -> dict[str, Any]:
    model = model.strip()
    if not model or len(model) > settings.model_max_chars or any(c.isspace() for c in model):
        raise ValueError("Invalid model identifier")
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            set_setting(connection, "model", model)
            set_setting(connection, "auto_reply", str(int(auto_reply)))
    return {"model": model, "auto_reply": auto_reply}


def list_chats(settings: Settings, offset: int = 0) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        rows = connection.execute(
            "SELECT c.*, p.instagram_username, l.name, l.phone, l.furniture_type, l.city, "
            "(SELECT text FROM messages WHERE chat_id = c.id ORDER BY id DESC LIMIT 1) "
            "AS last_text, (SELECT COUNT(*) FROM drafts WHERE chat_id = c.id "
            "AND status = 'pending') AS pending_drafts "
            "FROM chats c LEFT JOIN leads l ON l.chat_id = c.id "
            "LEFT JOIN chat_profiles p ON p.chat_id = c.id "
            "ORDER BY (SELECT MAX(id) FROM messages WHERE chat_id = c.id) DESC, c.id DESC "
            "LIMIT ? OFFSET ?",
            (settings.panel_page_size, offset),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
        }


def chat_detail(settings: Settings, chat_id: int, offset: int = 0) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        # Keep the snapshot consistent while the worker/webhooks write on other connections.
        connection.execute("BEGIN")
        chat = connection.execute(
            "SELECT c.*, p.instagram_username FROM chats c "
            "LEFT JOIN chat_profiles p ON p.chat_id = c.id WHERE c.id = ?",
            (chat_id,),
        ).fetchone()
        if chat is None:
            raise LookupError("Chat not found")
        messages = connection.execute(
            "SELECT m.*, ctx.post_url, ctx.post_description, ctx.post_owner, "
            "ctx.reply_block_reason FROM messages m LEFT JOIN message_context ctx "
            "ON ctx.message_id = m.id WHERE m.chat_id = ? "
            "ORDER BY julianday(m.occurred_at) DESC, m.id DESC LIMIT ? OFFSET ?",
            (chat_id, settings.panel_page_size, offset),
        ).fetchall()
        drafts = connection.execute(
            "SELECT * FROM drafts WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, settings.panel_page_size),
        ).fetchall()
        attempts = connection.execute(
            "SELECT a.*, EXISTS (SELECT 1 FROM outgoing_receipts r JOIN messages m "
            "ON m.id = r.message_id WHERE r.custom_id = a.request_id "
            "AND m.chat_id = a.chat_id AND m.text = a.text) AS confirmed "
            "FROM outgoing_attempts a WHERE a.chat_id = ? ORDER BY a.created_at DESC LIMIT ?",
            (chat_id, settings.panel_page_size),
        ).fetchall()
        return {
            "chat": dict(chat),
            "lead": asdict(get_lead(connection, chat_id)),
            "messages": [dict(row) for row in reversed(messages)],
            "message_total": connection.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0],
            "drafts": [dict(row) for row in drafts],
            "attempts": [dict(row) for row in attempts],
            "reply_block_reason": "comment_private_reply_unverified"
            if chat["source_type"] == "comment"
            else None,
        }


def set_pause(settings: Settings, chat_id: int, paused: bool) -> dict[str, bool]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "UPDATE chats SET paused = ? WHERE id = ?", (int(paused), chat_id)
                ).rowcount
                != 1
            ):
                raise LookupError("Chat not found")
            if paused:
                cancel_generation(connection, chat_id)
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
            "worker_enabled": bool(settings.openrouter_api_key),
        }
