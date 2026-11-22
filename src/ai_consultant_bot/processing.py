"""Single-backend durable queue processing, consultation and optional Direct sending."""

from __future__ import annotations

import asyncio
import sqlite3
from time import monotonic
from typing import Any

from loguru import logger
from starlette.concurrency import run_in_threadpool

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.consultation import Consultation, build_prompt, parse_consultation
from ai_consultant_bot.knowledge import KnowledgeBase
from ai_consultant_bot.messaging import send_text
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.storage import (
    claim_next_job,
    consultation_is_current,
    conversation_chat_ids,
    finish_consultation_job,
    interrupt_consultation,
    load_consultation_context,
    open_database,
    record_api_call,
    save_consultation,
    save_lead,
)
from ai_consultant_bot.umnico import UmnicoClient


def claim_consultation(settings: Settings) -> tuple[sqlite3.Row, dict] | None:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            job = claim_next_job(connection)
            if job is None:
                return None
            if not consultation_is_current(connection, job["id"], job["generation"]):
                finish_consultation_job(connection, job["id"], "done", job["generation"])
                return job, {}
            return job, load_consultation_context(connection, job, settings)


def finish_job(settings: Settings, job: sqlite3.Row, status: str) -> None:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            finish_consultation_job(connection, job["id"], status, job["generation"])


def finish_interrupted_job(settings: Settings, job: sqlite3.Row) -> None:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            interrupt_consultation(connection, job["id"], job["generation"])


async def generate_consultation(
    settings: Settings,
    knowledge: KnowledgeBase,
    openrouter: OpenRouterClient,
    context: dict[str, Any],
) -> Consultation:
    documents = knowledge.documents
    context = dict(context)
    model = context.pop("model")
    started = monotonic()
    error_code, status_code = None, None
    try:
        raw = await openrouter.complete(
            build_prompt(
                **context,
                knowledge=documents,
                reply_max_chars=settings.consultation_reply_max_chars,
                lead_field_max_chars=settings.lead_field_max_chars,
            ),
            model=model,
        )
        status_code = 200
        result = parse_consultation(raw, context["history"], settings, known_lead=context["lead"])
    except APIError as error:
        error_code = error.code
        if error.status_code is not None:
            status_code = error.status_code
        raise
    except asyncio.CancelledError:
        error_code = "cancelled"
        raise
    finally:
        with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
            with connection:
                record_api_call(
                    connection,
                    "consultation",
                    int((monotonic() - started) * 1000),
                    service="openrouter",
                    error_code=error_code,
                    status_code=status_code,
                )
    return result


def load_suggestion_context(
    connection: sqlite3.Connection, settings: Settings, chat_id: int
) -> dict[str, Any]:
    member_ids = conversation_chat_ids(connection, chat_id)
    placeholders = ",".join("?" for _ in member_ids)
    if (
        connection.execute(
            f"SELECT MIN(paused) FROM chats WHERE id IN ({placeholders})", member_ids
        ).fetchone()[0]
        != 1
    ):
        raise APIError("openrouter", "suggestion_requires_pause")
    message = connection.execute(
        f"SELECT id AS message_id, chat_id FROM messages WHERE chat_id IN ({placeholders}) "
        "AND direction = 'incoming' AND trim(text) != '' "
        "ORDER BY imported, julianday(occurred_at) DESC, id DESC LIMIT 1",
        member_ids,
    ).fetchone()
    if message is None:
        raise APIError("openrouter", "no_incoming_message")
    return load_consultation_context(connection, message, settings)


def read_suggestion_context(settings: Settings, chat_id: int) -> dict[str, Any]:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        connection.execute("BEGIN")
        return load_suggestion_context(connection, settings, chat_id)


def save_suggestion_lead(
    settings: Settings, chat_id: int, context: dict[str, Any], result: Consultation
) -> None:
    with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if load_suggestion_context(connection, settings, chat_id) != context:
                raise APIError("openrouter", "consultation_superseded")
            save_lead(connection, chat_id, result.lead, cleared_fields=result.cleared_fields)


async def generate_suggestion(
    settings: Settings,
    knowledge: KnowledgeBase,
    openrouter: OpenRouterClient,
    chat_id: int,
) -> str:
    context = await run_in_threadpool(read_suggestion_context, settings, chat_id)
    result = await generate_consultation(settings, knowledge, openrouter, context)
    await run_in_threadpool(save_suggestion_lead, settings, chat_id, context, result)
    return result.reply


async def process_next_job(
    settings: Settings,
    knowledge: KnowledgeBase,
    openrouter: OpenRouterClient,
    umnico: UmnicoClient,
) -> bool:
    claimed = claim_consultation(settings)
    if claimed is None:
        return False
    job, context = claimed
    if not context:
        return True
    try:
        result = await generate_consultation(settings, knowledge, openrouter, context)
        with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                paused = save_consultation(
                    connection,
                    job,
                    result.lead,
                    cleared_fields=result.cleared_fields,
                )
                if paused is not None and (paused or context["source_type"] == "comment"):
                    finish_consultation_job(connection, job["id"], "done", job["generation"])
        if paused is None:
            logger.info("Консультация устарела до сохранения ответа.")
            return True
        if paused or context["source_type"] == "comment":
            logger.info("Консультация и заявка сохранены.")
            return True
        try:
            attempt = await send_text(
                settings,
                umnico,
                chat_id=job["chat_id"],
                text=result.reply,
                request_id=f"consultation:{job['id']}",
                job_id=job["id"],
                generation=job["generation"],
            )
        except APIError as error:
            if error.code == "consultation_superseded":
                finish_job(settings, job, "done")
                return True
            raise
        status = attempt["status"]
        finish_job(settings, job, "unknown" if status == "unknown" else "done")
        return True
    except (Exception, asyncio.CancelledError):
        finish_interrupted_job(settings, job)
        raise


async def run_worker(
    settings: Settings,
    knowledge: KnowledgeBase,
    openrouter: OpenRouterClient,
    umnico: UmnicoClient,
) -> None:
    while True:
        try:
            if await process_next_job(settings, knowledge, openrouter, umnico):
                await asyncio.sleep(0)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log exception details, prompts, PII, paths or API response bodies.
            logger.error("Обработка консультации не выполнена; подробности доступны в SQLite.")
        await asyncio.sleep(settings.processing_poll_interval)
