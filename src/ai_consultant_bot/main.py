"""Argument-free startup: settings, tunnel prompt, server, webhook registration."""

from __future__ import annotations

import asyncio

import httpx
import uvicorn
from loguru import logger

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings, load_settings
from ai_consultant_bot.logging_config import configure_logging
from ai_consultant_bot.umnico import UmnicoClient
from ai_consultant_bot.web import create_app
from ai_consultant_bot.webhooks import build_webhook_url


def prompt_tunnel_url(token: str) -> str:
    while True:
        value = input("Текущий HTTPS-адрес туннеля tuna (без пути): ").strip()
        try:
            build_webhook_url(value, token)
        except ValueError:
            print("Укажите публичный HTTPS-адрес вида https://your-tunnel.example.com")
        else:
            return value


async def register_when_ready(
    server: uvicorn.Server,
    settings: Settings,
    webhook_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    async with asyncio.timeout(settings.server_startup_timeout):
        while not server.started:
            if server.should_exit:
                raise RuntimeError("Server did not start")
            await asyncio.sleep(0.05)
    async with UmnicoClient(settings, transport=transport) as client:
        result = await client.ensure_webhook(webhook_url)
    if result.created:
        logger.info("Webhook создан, ID {}.", result.webhook_id)
    else:
        logger.info("Webhook уже зарегистрирован, ID {}.", result.webhook_id)


async def serve_backend(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except SystemExit:
        # Uvicorn exits on bind/lifespan failure; keep cleanup in our event loop.
        raise RuntimeError("Server startup failed") from None


async def run_application(settings: Settings, tunnel_url: str) -> int:
    """Run the server before registering its callback; stop cleanly on failure."""
    webhook_url = build_webhook_url(tunnel_url, settings.webhook_token)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=settings.server_host,
            port=settings.server_port,
            access_log=False,
            proxy_headers=False,
            timeout_graceful_shutdown=settings.server_shutdown_timeout,
        )
    )
    server_task = asyncio.create_task(serve_backend(server))
    registration_task = asyncio.create_task(register_when_ready(server, settings, webhook_url))
    try:
        done, _ = await asyncio.wait(
            (server_task, registration_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if registration_task not in done:
            await server_task
            return 1
        await registration_task
        logger.info("AI consultant bot is ready.")
        await server_task
        return 0
    except APIError as error:
        logger.error("Регистрация webhook не выполнена: {}.", error)
        return 1
    except (OSError, RuntimeError, TimeoutError):
        logger.error("Не удалось запустить сервер или дождаться его готовности.")
        return 1
    finally:
        registration_task.cancel()
        await asyncio.gather(registration_task, return_exceptions=True)
        server.should_exit = True
        try:
            async with asyncio.timeout(settings.server_shutdown_timeout):
                await asyncio.wait((server_task,))
        except TimeoutError:
            server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)


def main(settings: Settings | None = None, tunnel_url: str | None = None) -> int:
    """Python arguments support embedding/testing; no command-line arguments are read."""
    try:
        settings = load_settings() if settings is None else settings
    except (OSError, ValueError):
        logger.error("Не удалось загрузить секреты: проверьте .env и пару login/password.")
        return 1
    configure_logging(settings.log_level)
    if not settings.webhook_token or not (settings.umnico_login or settings.umnico_api_key):
        logger.error("Заполните WEBHOOK_TOKEN и учётные данные Umnico в .env.")
        return 1
    try:
        build_webhook_url("https://tunnel.example.com", settings.webhook_token)
        tunnel_url = prompt_tunnel_url(settings.webhook_token) if tunnel_url is None else tunnel_url
        return asyncio.run(run_application(settings, tunnel_url))
    except (EOFError, KeyboardInterrupt):
        logger.info("Запуск остановлен пользователем.")
        return 0
    except ValueError:
        logger.error("Некорректный адрес туннеля или формат WEBHOOK_TOKEN.")
        return 1
