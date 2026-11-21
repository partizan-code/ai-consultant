"""Offline staging checks: synthetic secrets, temporary data, ASGI and forbidden egress."""

import asyncio
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.umnico import UmnicoClient
from ai_consultant_bot.web import create_app
from tests.test_web import incoming


def staging_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="staging",
        database_path=tmp_path / "staging.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        admin_token="synthetic-admin",
        webhook_token="synthetic-hook",
        umnico_login="synthetic-login",
        umnico_password="synthetic-password",
        openrouter_api_key="synthetic-openrouter",
    )


def test_staging_blocks_both_clients_even_with_credentials_and_transport(tmp_path: Path) -> None:
    settings = staging_settings(tmp_path)

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Staging allowed an external request")

    async def run() -> None:
        async with (
            UmnicoClient(settings, transport=httpx.MockTransport(forbidden)) as umnico,
            OpenRouterClient(settings, transport=httpx.MockTransport(forbidden)) as router,
        ):
            for operation in (
                umnico.list_integrations(),
                umnico.ensure_webhook("https://example.org/webhooks/umnico/synthetic-hook"),
                umnico.send_text(
                    lead_id="456",
                    source_real_id="255",
                    integration_id="88",
                    text="Synthetic",
                    custom_id="synthetic-send",
                ),
                router.complete([{"role": "user", "content": "Synthetic"}]),
            ):
                with pytest.raises(APIError, match="external_api_disabled"):
                    await operation

    asyncio.run(run())


def test_staging_accepts_synthetic_events_but_never_sends(tmp_path: Path) -> None:
    settings = staging_settings(tmp_path)

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unexpected external request or worker")

    app = create_app(settings, transport=httpx.MockTransport(forbidden))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver"
            ) as client:
                response = await client.post("/webhooks/umnico/synthetic-hook", json=incoming())
                assert response.json() == {"status": "saved"}
                headers = {"Authorization": "Bearer synthetic-admin"}
                status = (await client.get("/admin/status", headers=headers)).json()
                assert not status["worker_enabled"]
                response = await client.post(
                    "/admin/chats/1/send-text",
                    headers=headers,
                    json={"text": "Synthetic", "request_id": "synthetic-send"},
                )
                assert response.status_code == 200
                assert response.json()["status"] == "failed"
                assert response.json()["error_code"] == "external_api_disabled"
                response = await client.post("/admin/chats/1/import-history", headers=headers)
                assert response.status_code == 409
                assert response.json()["error_code"] == "external_api_disabled"

    asyncio.run(run())
