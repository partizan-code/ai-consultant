"""Single-backend durable queue processing, consultation and optional Direct sending."""

from __future__ import annotations

import asyncio
import sqlite3
from time import monotonic

from loguru import logger

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.consultation import build_prompt, parse_consultation
from ai_consultant_bot.knowledge import KnowledgeBase
from ai_consultant_bot.messaging import send_text
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.storage import (
    claim_next_job,
    consultation_is_current,
    finish_consultation_job,
    interrupt_consultation,
    load_consultation_context,
    open_database,
    record_api_call,
    save_consultation,
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
        documents = knowledge.documents
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
            result = parse_consultation(
                raw, context["history"], settings, known_lead=context["lead"]
            )
        except APIError as error:
            error_code = error.code
            if error.status_code is not None:
                status_code = error.status_code
            raise
        except asyncio.CancelledError:
            error_code = "cancelled"
            raise
        finally:
            with open_database(
                settings.database_path, timeout=settings.database_timeout
            ) as connection:
                with connection:
                    record_api_call(
                        connection,
                        "consultation",
                        int((monotonic() - started) * 1000),
                        service="openrouter",
                        error_code=error_code,
                        status_code=status_code,
                    )
        with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                paused = save_consultation(
                    connection,
                    job,
                    result.lead,
                    result.reply,
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
