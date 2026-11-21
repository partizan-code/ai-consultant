"""ASGI-only API checks: no listening sockets, credentials or live endpoints."""

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from loguru import logger

from ai_consultant_bot.config import Settings
from ai_consultant_bot.storage import open_database
from ai_consultant_bot.web import create_app


def incoming() -> dict:
    return {
        "type": "message.incoming",
        "accountId": 123,
        "leadId": 456,
        "message": {
            "messageId": "test",
            "datetime": "2026-11-20T10:00:00Z",
            "incoming": True,
            "sa": {"id": 88},
            "source": {"id": "sender", "realId": 255, "type": "message"},
            "message": {"text": "Нужен диван"},
        },
    }


def test_webhook_auth_validation_and_deduplication(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        webhook_token="synthetic-hook",
        admin_token="synthetic-admin",
        webhook_max_bytes=4096,
        umnico_history_verified=False,
        umnico_send_verified=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Receiving a webhook must not call external APIs")

    app = create_app(settings, transport=httpx.MockTransport(handler))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                assert (await client.get("/health")).status_code == 200
                assert (
                    await client.post("/webhooks/umnico/wrong", json=incoming())
                ).status_code == 401
                url = "/webhooks/umnico/synthetic-hook"
                assert (await client.post(url, content=b"invalid")).status_code == 400
                assert (await client.post(url, content=b"x" * 4097)).status_code == 413
                assert (await client.post(url, json=incoming())).json() == {"status": "saved"}
                assert (await client.post(url, json=incoming())).json() == {"status": "duplicate"}
                assert (await client.post(url, json={"type": "lead.changed"})).json() == {
                    "status": "ignored",
                }
                history = "/admin/chats/1/import-history"
                assert (await client.post(history)).status_code == 401
                headers = {"Authorization": "Bearer synthetic-admin"}
                response = await client.post(history, headers=headers)
                assert response.status_code == 409
                assert response.json()["error_code"] == "history_contract_unverified"
                response = await client.post(
                    "/admin/chats/1/send-text",
                    headers=headers,
                    json={"text": "reply", "request_id": "one"},
                )
                assert response.json()["status"] == "blocked"
                assert "synthetic-" not in response.text
            assert not app.state.umnico._http.is_closed
        assert app.state.umnico._http.is_closed

    asyncio.run(run())


def test_missing_tokens_disable_endpoints(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3", knowledge_base_path=tmp_path / "knowledge"
    )
    app = create_app(replace(settings, webhook_max_bytes=10))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                assert (await client.post("/webhooks/umnico/anything")).status_code == 503
                assert (await client.post("/admin/chats/1/import-history")).status_code == 503

    asyncio.run(run())


def test_webhook_logs_results_without_secrets_or_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        webhook_token="private-hook",
    )
    app = create_app(settings)
    logs = []
    sink = logger.add(logs.append, format="{level} | {message}")

    def storage_failure(*args: object) -> str:
        raise sqlite3.OperationalError("private-database-detail")

    async def run() -> None:
        event = incoming()
        event["message"]["messageId"] = "private-message-id"
        event["message"]["message"]["text"] = "private-message-text"
        event["message"]["source"]["token"] = "private-source-token"
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                url = "/webhooks/umnico/private-hook"
                assert (await client.post(url, json=event)).json() == {"status": "saved"}
                assert (await client.post(url, json=event)).json() == {"status": "duplicate"}
                assert (
                    await client.post(url, json={"type": "private-event\nforged-line"})
                ).json() == {
                    "status": "ignored",
                }
                assert (await client.post(url, content=b"private-invalid-json")).status_code == 400
                assert (await client.post(url, json={})).status_code == 400
                assert (
                    await client.post("/webhooks/umnico/private-wrong-token")
                ).status_code == 401
                monkeypatch.setattr("ai_consultant_bot.web.ingest_event", storage_failure)
                assert (await client.post(url, json=event)).status_code == 503

    try:
        asyncio.run(run())
    finally:
        logger.remove(sink)
    output = "\n".join(logs)
    assert "сохранено" in output
    assert "в очередь" in output
    assert "повторная доставка" in output
    assert "пропущено" in output
    assert "WARNING" in output and "ERROR" in output
    assert "private-" not in output and "forged-line" not in output
    assert "/webhooks/" not in output


def test_history_send_and_outgoing_webhook_through_admin_api(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        umnico_api_key="synthetic-key",
        admin_token="synthetic-admin",
        webhook_token="synthetic-hook",
        umnico_user_id="15",
    )
    calls = []
    old = incoming()["message"]
    old["messageId"] = "old"
    old["sentAt"] = old.pop("datetime")

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/history/255"):
            first = not body
            return httpx.Response(
                200,
                json={
                    "source": 255 if first else 0,
                    "cursor": 100 if first else 0,
                    "messages": [old] if first else [],
                },
            )
        assert request.url.path == "/v2/messaging/456/messages"
        assert body == {
            "source": 255,
            "saId": 88,
            "userId": 15,
            "customId": "request-1",
            "message": {"text": "Здравствуйте"},
        }
        return httpx.Response(200, json=[{"messageId": "sent"}])

    app = create_app(settings, transport=httpx.MockTransport(handler))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                hook = "/webhooks/umnico/synthetic-hook"
                headers = {"Authorization": "Bearer synthetic-admin"}
                assert (await client.post(hook, json=incoming())).json() == {"status": "saved"}
                result = await client.post("/admin/chats/1/import-history", headers=headers)
                assert result.status_code == 200 and result.json() == {"imported": 1}
                replay = incoming()
                replay["message"]["messageId"] = "old"
                assert (await client.post(hook, json=replay)).json() == {"status": "duplicate"}
                send_url = "/admin/chats/1/send-text"
                body = {"text": "Здравствуйте", "request_id": "request-1"}
                result = await client.post(send_url, json=body, headers=headers)
                assert result.json()["status"] == "sent"
                receipt = incoming()
                receipt["type"] = "message.outgoing"
                receipt["message"].update(
                    {"incoming": False, "messageId": "sent", "customId": "request-1"}
                )
                receipt["message"]["message"]["text"] = "Здравствуйте"
                assert (await client.post(hook, json=receipt)).json() == {"status": "saved"}
                assert (
                    await client.post(send_url, json=body, headers=headers)
                ).json() == result.json()
                conflict = await client.post(
                    send_url, headers=headers, json={**body, "text": "changed"}
                )
                assert conflict.status_code == 409

    asyncio.run(run())
    assert len(calls) == 3
    with open_database(settings.database_path, timeout=1) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM messages WHERE imported = 1").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT custom_id FROM outgoing_receipts").fetchone()[0]
            == "request-1"
        )
        assert connection.execute("SELECT paused FROM chats").fetchone()[0] == 1


def test_worker_lifecycle_and_authenticated_knowledge_reload(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        openrouter_api_key="synthetic-key",
        webhook_token="synthetic-hook",
        admin_token="synthetic-admin",
        processing_poll_interval=0.01,
    )
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "sofa.md").write_text("Диван: велюр.", encoding="utf-8")
    completed = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("chat/completions")
        completed.set()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"reply":"Обивка велюр.","lead":{}}'},
                    }
                ]
            },
        )

    app = create_app(settings, transport=httpx.MockTransport(handler))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                url = "/admin/knowledge/reload"
                assert (await client.post(url)).status_code == 401
                headers = {"Authorization": "Bearer synthetic-admin"}
                assert (await client.post(url, headers=headers)).json() == {"chunks": 1}
                (settings.knowledge_base_path / "invalid.txt").write_bytes(b"\xff")
                assert (await client.post(url, headers=headers)).status_code == 409
                assert len(app.state.knowledge.chunks) == 1
                await client.post("/webhooks/umnico/synthetic-hook", json=incoming())
                await asyncio.wait_for(completed.wait(), timeout=2)
                with open_database(settings.database_path, timeout=1) as connection:
                    assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "done"
                    assert (
                        connection.execute("SELECT text FROM suggestions").fetchone()[0]
                        == "Обивка велюр."
                    )
        assert app.state.openrouter._http.is_closed and app.state.umnico._http.is_closed

    asyncio.run(run())
