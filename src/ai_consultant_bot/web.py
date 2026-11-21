"""Thin FastAPI endpoints and lifecycle for the single-backend consultation worker."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.concurrency import run_in_threadpool

from ai_consultant_bot.admin import (
    chat_detail,
    list_chats,
    read_settings,
    service_status,
    set_pause,
    update_settings,
)
from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.knowledge import KnowledgeBase, KnowledgeLimitError
from ai_consultant_bot.messaging import (
    import_conversation_history,
    ingest_event,
    send_text,
)
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.processing import run_worker
from ai_consultant_bot.storage import initialize_database, open_database, recover_interrupted_jobs
from ai_consultant_bot.umnico import UmnicoClient


async def read_payload(request: Request, limit: int) -> Any:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, "Request body too large")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Invalid JSON") from None


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        with open_database(settings.database_path, timeout=settings.database_timeout) as connection:
            initialize_database(connection, settings)
            with connection:
                recover_interrupted_jobs(connection)
        logger.info("SQLite готова к работе.")
        knowledge = KnowledgeBase(settings)
        try:
            await run_in_threadpool(knowledge.reload)
        except (OSError, ValueError):
            logger.warning(
                "База знаний не загружена; консультации доступны без сведений о товарах."
            )
        app.state.knowledge = knowledge
        async with (
            UmnicoClient(settings, transport=transport) as client,
            OpenRouterClient(settings, transport=transport) as openrouter,
        ):
            app.state.umnico = client
            app.state.openrouter = openrouter
            worker = None
            if settings.openrouter_api_key and settings.external_apis_enabled:
                worker = asyncio.create_task(run_worker(settings, knowledge, openrouter, client))
                logger.info("Обработчик консультаций запущен.")
            else:
                logger.warning("Обработчик консультаций выключен; задания остаются в очереди.")
            try:
                yield
            finally:
                if worker is not None:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def require_admin(request: Request) -> None:
        if not settings.admin_token:
            raise HTTPException(503, "Admin token is not configured")
        expected = f"Bearer {settings.admin_token}".encode()
        actual = request.headers.get("Authorization", "").encode()
        if not secrets.compare_digest(expected, actual):
            raise HTTPException(401, "Unauthorized")

    @app.exception_handler(APIError)
    async def api_error(request: Request, error: APIError) -> JSONResponse:
        logger.warning("Операция API не выполнена: {}.", error)
        return JSONResponse(status_code=409, content={"error_code": error.code})

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error: sqlite3.Error) -> JSONResponse:
        logger.error("Ошибка SQLite: операция не выполнена (HTTP 503).")
        return JSONResponse(status_code=503, content={"error_code": "storage_unavailable"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin/settings", dependencies=[Depends(require_admin)])
    def get_settings() -> dict[str, Any]:
        return read_settings(settings)

    @app.put("/admin/settings", dependencies=[Depends(require_admin)])
    async def put_settings(request: Request) -> dict[str, Any]:
        payload = await read_payload(request, settings.webhook_max_bytes)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"model"}
            or not isinstance(payload["model"], str)
        ):
            raise HTTPException(400, "Model is required")
        try:
            return await run_in_threadpool(update_settings, settings, payload["model"])
        except ValueError:
            raise HTTPException(400, "Invalid model identifier") from None

    @app.get("/admin/chats", dependencies=[Depends(require_admin)])
    def chats(offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        return list_chats(settings, offset)

    @app.get("/admin/chats/{chat_id}", dependencies=[Depends(require_admin)])
    def detail(chat_id: int, offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
        try:
            return chat_detail(settings, chat_id, offset)
        except LookupError:
            raise HTTPException(404, "Chat not found") from None

    @app.post("/admin/chats/{chat_id}/pause", dependencies=[Depends(require_admin)])
    async def pause(chat_id: int, request: Request) -> dict[str, bool]:
        payload = await read_payload(request, settings.webhook_max_bytes)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"paused"}
            or type(payload["paused"]) is not bool
        ):
            raise HTTPException(400, "Boolean paused is required")
        try:
            return await run_in_threadpool(set_pause, settings, chat_id, payload["paused"])
        except LookupError:
            raise HTTPException(404, "Chat not found") from None

    @app.get("/admin/status", dependencies=[Depends(require_admin)])
    def status() -> dict[str, Any]:
        return service_status(settings)

    @app.post("/admin/knowledge/reload", dependencies=[Depends(require_admin)])
    async def reload_knowledge(request: Request) -> dict[str, int]:
        try:
            count = await run_in_threadpool(request.app.state.knowledge.reload)
        except KnowledgeLimitError as exc:
            raise HTTPException(
                409,
                {
                    "error_code": "knowledge_limit_exceeded",
                    "message": f"{exc} Предыдущая версия базы знаний сохранена.",
                },
            ) from None
        except (OSError, ValueError):
            raise HTTPException(
                409, "Knowledge reload failed; previous snapshot retained"
            ) from None
        logger.info("База знаний обновлена: документов — {}.", count)
        return {"documents": count}

    @app.post("/webhooks/umnico/{token}")
    async def webhook(token: str, request: Request) -> dict[str, str]:
        # URL capability until Umnico's signing contract is known. Disable access logs.
        if not settings.webhook_token:
            logger.warning("Webhook отклонён: токен приёма не настроен (HTTP 503).")
            raise HTTPException(503, "Webhook token is not configured")
        if not secrets.compare_digest(token.encode(), settings.webhook_token.encode()):
            logger.warning("Webhook отклонён: неверный токен (HTTP 401).")
            raise HTTPException(401, "Unauthorized")
        try:
            payload = await read_payload(request, settings.webhook_max_bytes)
        except HTTPException as error:
            logger.warning(
                "Webhook отклонён: некорректное тело запроса (HTTP {}).", error.status_code
            )
            raise
        try:
            result = await run_in_threadpool(ingest_event, settings, payload)
        except ValueError:
            logger.warning("Webhook отклонён: некорректная структура события (HTTP 400).")
            raise HTTPException(400, "Invalid Umnico event") from None
        return {"status": result}

    @app.post("/admin/chats/{chat_id}/import-history", dependencies=[Depends(require_admin)])
    async def history(chat_id: int, request: Request) -> dict[str, int]:
        try:
            return {
                "imported": await import_conversation_history(
                    settings, request.app.state.umnico, chat_id
                )
            }
        except LookupError:
            raise HTTPException(404, "Chat not found") from None

    @app.post("/admin/chats/{chat_id}/send-text", dependencies=[Depends(require_admin)])
    async def send(chat_id: int, request: Request) -> dict[str, Any]:
        payload = await read_payload(request, settings.webhook_max_bytes)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("text"), str)
            or not isinstance(payload.get("request_id"), str)
            or len(payload["request_id"]) > 128
        ):
            raise HTTPException(400, "Text and request_id are required")
        try:
            result = await send_text(
                settings,
                request.app.state.umnico,
                chat_id=chat_id,
                text=payload["text"],
                request_id=payload["request_id"],
            )
        except LookupError:
            raise HTTPException(404, "Chat not found") from None
        except ValueError:
            raise HTTPException(409, "Invalid text or conflicting request_id") from None
        return {key: result[key] for key in ("request_id", "status", "error_code")}

    return app
