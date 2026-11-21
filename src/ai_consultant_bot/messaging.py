"""Event ingestion, explicitly requested history import and durable text sends."""

from __future__ import annotations

from time import monotonic
from typing import Any

from loguru import logger

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.events import parse_event, parse_history_message
from ai_consultant_bot.storage import (
    cancel_generation,
    clear_answered_suggestion,
    consultation_is_current,
    conversation_chat_ids,
    open_database,
    reconcile_outgoing_attempt,
    record_api_call,
    store_event,
)
from ai_consultant_bot.umnico import UmnicoClient


def ingest_event(settings: Settings, payload: Any) -> str:
    event = parse_event(payload)
    if event is None:
        known_types = {"customer.created", "lead.created", "lead.changed"}
        event_type = payload["type"] if payload["type"] in known_types else "unknown"
        logger.info("Webhook: событие {} пропущено.", event_type)
        return "ignored"
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = store_event(connection, event)
            correlated = (
                event.custom_id is not None
                and connection.execute(
                    "SELECT 1 FROM outgoing_attempts a "
                    "JOIN outgoing_receipts r ON r.custom_id = a.request_id "
                    "JOIN messages m ON m.id = r.message_id JOIN chats c ON c.id = m.chat_id "
                    "WHERE a.request_id = ? AND a.status = 'sent' AND a.chat_id = m.chat_id "
                    "AND a.text = m.text AND m.external_id = ? AND m.text = ? "
                    "AND c.account_id = ? AND c.lead_id = ? AND c.integration_id = ? "
                    "AND c.source_type = ? AND c.source_id = ?",
                    (
                        event.custom_id,
                        event.external_id,
                        event.text,
                        event.chat.account_id,
                        event.chat.lead_id,
                        event.chat.integration_id,
                        event.chat.source_type,
                        event.chat.source_id,
                    ),
                ).fetchone()
                is not None
            )
    if correlated:
        logger.info("Исходящее сообщение сопоставлено с попыткой отправки.")
    if not saved:
        logger.info("Webhook: повторная доставка сообщения пропущена.")
        return "duplicate"
    direction = "входящее" if event.direction == "incoming" else "исходящее"
    source = "Direct" if event.chat.source_type == "message" else "комментарий"
    logger.info("Webhook: {} сообщение ({}) сохранено.", direction, source)
    if event.direction == "incoming" and event.text.strip():
        logger.info("Задание добавлено в очередь, ожидает обработки.")
    return "saved"


def get_chat_route(settings: Settings, chat_id: int) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        chat = connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if chat is None:
            raise LookupError("Chat not found")
        context = connection.execute(
            "SELECT ctx.source_real_id FROM message_context ctx JOIN messages m "
            "ON m.id = ctx.message_id WHERE m.chat_id = ? AND ctx.source_real_id IS NOT NULL "
            "ORDER BY m.id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return {**dict(chat), "source_real_id": context[0] if context else None}


async def resolve_chat_route(
    settings: Settings, client: UmnicoClient, chat_id: int
) -> dict[str, Any]:
    route = get_chat_route(settings, chat_id)
    if route["source_real_id"] is None:
        threads = await client.list_threads(route["lead_id"])
        matches = {
            str(thread["realId"])
            for thread in threads
            if thread["id"] == route["source_id"]
            and thread["type"] == route["source_type"]
            and str(thread["saId"]) == route["integration_id"]
            and thread["realId"] is not None
        }
        if not matches:
            raise APIError("umnico", "missing_source_real_id")
        if len(matches) != 1:
            raise APIError("umnico", "ambiguous_source_real_id")
        route["source_real_id"] = matches.pop()
    return route


async def import_conversation_history(
    settings: Settings, client: UmnicoClient, chat_id: int
) -> int:
    """Import each preserved source, also backfilling previews on duplicate messages."""
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        member_ids = conversation_chat_ids(connection, chat_id)
    count = 0
    for member_id in member_ids:
        count += await import_history(settings, client, member_id)
    return count


async def import_history(settings: Settings, client: UmnicoClient, chat_id: int) -> int:
    if not settings.umnico_history_verified:
        raise APIError("umnico", "history_contract_unverified")
    route = await resolve_chat_route(settings, client, chat_id)
    cursor: int | None = None
    seen: set[int] = set()
    count = 0
    for _ in range(settings.history_max_pages):
        started = monotonic()
        error = None
        try:
            page = await client.history_page(
                route["lead_id"],
                route["source_real_id"],
                cursor=cursor,
            )
            if any(item.get("messageId") is None for item in page["messages"]):
                raise APIError("umnico", "history_message_id_missing")
            try:
                events = [parse_history_message(route, item) for item in page["messages"]]
            except ValueError:
                raise APIError("umnico", "invalid_history_message") from None
            if any(
                event.chat.integration_id != route["integration_id"]
                or event.chat.source_type != route["source_type"]
                or event.chat.source_id != route["source_id"]
                or event.source_real_id != route["source_real_id"]
                for event in events
            ):
                raise APIError("umnico", "history_source_mismatch")
            next_cursor = page["cursor"]
            if next_cursor != 0 and next_cursor in seen:
                raise APIError("umnico", "history_cursor_loop")
            with open_database(
                settings.database_path, timeout=settings.database_timeout
            ) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    count += sum(store_event(connection, event, imported=True) for event in events)
        except APIError as caught:
            error = caught
            raise
        except ValueError:
            error = APIError("umnico", "invalid_history_message")
            raise error from None
        finally:
            with (
                open_database(
                    settings.database_path, timeout=settings.database_timeout
                ) as connection,
                connection,
            ):
                record_api_call(
                    connection,
                    "history",
                    int((monotonic() - started) * 1000),
                    error_code=error.code if error else None,
                    status_code=error.status_code if error else None,
                )
        if next_cursor == 0:
            logger.info("Импорт истории завершён: добавлено сообщений — {}.", count)
            return count
        seen.add(next_cursor)
        cursor = next_cursor
    raise APIError("umnico", "history_page_limit")


async def send_text(
    settings: Settings,
    client: UmnicoClient,
    *,
    chat_id: int,
    text: str,
    request_id: str,
    job_id: int | None = None,
    generation: int | None = None,
) -> dict[str, Any]:
    """Persist uncertainty BEFORE I/O; never retry a request ID.

    New IDs also cannot bypass an unresolved send in this chat, including after restart.
    'sent' means API acceptance, not confirmed delivery to Instagram.
    An internal job_id selects automatic sending and must still own current work.
    """
    if (
        not text.strip()
        or not request_id.strip()
        or request_id != request_id.strip()
        or len(request_id) > 128
        or (job_id is None and request_id.startswith("consultation:"))
        or (job_id is not None and (generation is None or request_id != f"consultation:{job_id}"))
    ):
        raise ValueError("Text and request ID are required")
    # A replay must return durable state even if a route lookup would now fail.
    # The reservation transaction below repeats these checks after any GET I/O.
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        previous = connection.execute(
            "SELECT * FROM outgoing_attempts WHERE request_id = ?", (request_id,)
        ).fetchone()
        if previous is not None:
            if previous["chat_id"] != chat_id or previous["text"] != text:
                raise ValueError("Request ID already used with different content")
            return dict(previous)
        member_ids = conversation_chat_ids(connection, chat_id)
        placeholders = ",".join("?" for _ in member_ids)
        if connection.execute(
            f"SELECT 1 FROM outgoing_attempts WHERE chat_id IN ({placeholders}) "
            "AND status = 'unknown'",
            member_ids,
        ).fetchone():
            raise APIError("umnico", "unresolved_send")
    if job_id is None:
        with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for member_id in conversation_chat_ids(connection, chat_id):
                    connection.execute("UPDATE chats SET paused = 1 WHERE id = ?", (member_id,))
                    cancel_generation(connection, member_id)
    route = get_chat_route(settings, chat_id)
    blocked = None
    if route["source_type"] == "comment":
        blocked = "comment_private_reply_unverified"
    elif not settings.umnico_send_verified:
        blocked = "send_contract_unverified"
    elif route["source_real_id"] is None:
        try:
            route = await resolve_chat_route(settings, client, chat_id)
        except APIError as error:
            if error.code not in {"missing_source_real_id", "ambiguous_source_real_id"}:
                raise
            blocked = error.code
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            member_ids = conversation_chat_ids(connection, chat_id)
            placeholders = ",".join("?" for _ in member_ids)
            reconcile_outgoing_attempt(connection, request_id)
            previous = connection.execute(
                "SELECT * FROM outgoing_attempts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if previous is not None:
                if previous["chat_id"] != chat_id or previous["text"] != text:
                    raise ValueError("Request ID already used with different content")
                return dict(previous)
            if connection.execute(
                f"SELECT 1 FROM outgoing_attempts WHERE chat_id IN ({placeholders}) "
                "AND status = 'unknown'",
                member_ids,
            ).fetchone():
                raise APIError("umnico", "unresolved_send")
            if (
                job_id is not None
                and connection.execute(
                    "SELECT 1 FROM jobs j JOIN messages m ON m.id = j.message_id "
                    f"WHERE m.chat_id IN ({placeholders}) AND j.status IN ('processing', 'unknown') "
                    "AND (? IS NULL OR j.id != ?)",
                    (*member_ids, job_id, job_id),
                ).fetchone()
            ):
                raise APIError("umnico", "chat_processing")
            if job_id is not None:
                owner = connection.execute(
                    "SELECT 1 FROM jobs j JOIN messages m ON m.id = j.message_id "
                    "WHERE j.id = ? AND m.chat_id = ?",
                    (job_id, chat_id),
                ).fetchone()
                if owner is None or not consultation_is_current(connection, job_id, generation):
                    raise APIError("umnico", "consultation_superseded")
                if connection.execute(
                    f"SELECT 1 FROM chats WHERE id IN ({placeholders}) AND paused = 1", member_ids
                ).fetchone():
                    raise APIError("umnico", "consultation_superseded")
            latest = connection.execute(
                f"SELECT MAX(id) FROM messages WHERE chat_id IN ({placeholders}) "
                "AND direction = 'incoming' AND imported = 0",
                member_ids,
            ).fetchone()[0]
            # Invalidate any generation claimed during a manual route lookup too.
            if job_id is None:
                for member_id in member_ids:
                    connection.execute("UPDATE chats SET paused = 1 WHERE id = ?", (member_id,))
                    cancel_generation(connection, member_id)
            connection.execute(
                "INSERT INTO outgoing_attempts(request_id, chat_id, message_id, text, status, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, chat_id, latest, text, "blocked" if blocked else "unknown", blocked),
            )
            if not blocked:
                reconcile_outgoing_attempt(connection, request_id)
                confirmed = connection.execute(
                    "SELECT * FROM outgoing_attempts WHERE request_id = ? AND status = 'sent'",
                    (request_id,),
                ).fetchone()
                if confirmed is not None:
                    return dict(confirmed)
        if blocked:
            return {"request_id": request_id, "status": "blocked", "error_code": blocked}
        started = monotonic()
        status, error_code, status_code = "unknown", "interrupted", None
        try:
            await client.send_text(
                lead_id=route["lead_id"],
                source_real_id=route["source_real_id"],
                integration_id=route["integration_id"],
                text=text,
                custom_id=request_id,
            )
            status, error_code = "sent", None
        except APIError as error:
            error_code, status_code = error.code, error.status_code
            # 5xx, transport failures and malformed successful responses are ambiguous.
            if (
                status_code is not None and 400 <= status_code < 500 and status_code != 408
            ) or error.code in {
                "send_contract_unverified",
                "external_api_disabled",
                "missing_or_invalid_sender",
                "missing_or_invalid_api_key",
                "invalid_messaging_identifier",
            }:
                status = "failed"
        finally:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE outgoing_attempts SET status = ?, error_code = ? "
                    "WHERE request_id = ? AND status = 'unknown'",
                    (status, error_code, request_id),
                )
                reconcile_outgoing_attempt(connection, request_id)
                result = dict(
                    connection.execute(
                        "SELECT * FROM outgoing_attempts WHERE request_id = ?", (request_id,)
                    ).fetchone()
                )
                if result["status"] == "sent":
                    clear_answered_suggestion(connection, request_id)
                record_api_call(
                    connection,
                    "send_text",
                    int((monotonic() - started) * 1000),
                    status_code=status_code,
                    error_code=error_code,
                )
        if result["status"] == "sent":
            logger.info("Отправка: сообщение принято Umnico.")
        else:
            logger.warning("Отправка завершена со статусом {}.", result["status"])
        return result
