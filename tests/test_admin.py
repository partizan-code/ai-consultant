"""Administrative workflows on temporary SQLite, ASGI and mocked integrations only."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.admin import chat_detail, set_pause
from ai_consultant_bot.config import Settings
from ai_consultant_bot.knowledge import KnowledgeBase
from ai_consultant_bot.messaging import ingest_event
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.processing import process_next_job
from ai_consultant_bot.storage import (
    Lead,
    initialize_database,
    open_database,
    save_lead,
)
from ai_consultant_bot.umnico import UmnicoClient
from ai_consultant_bot.web import create_app

HEADERS = {"Authorization": "Bearer synthetic-admin"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        admin_token="synthetic-admin",
        webhook_token="synthetic-hook",
        umnico_api_key="synthetic-umnico",
        umnico_user_id="15",
        panel_page_size=2,
    )
    with open_database(settings.database_path, timeout=1) as connection:
        initialize_database(connection, settings)
    return settings


def event(message_id: str = "first", source: str = "message") -> dict:
    return {
        "type": "message.incoming",
        "accountId": 123,
        "leadId": 456,
        "message": {
            "messageId": message_id,
            "datetime": "2026-11-20T10:00:00Z",
            "incoming": True,
            "sa": {"id": 88},
            "source": {"id": "sender", "realId": 255, "type": source},
            "message": {"text": "Нужен диван", "url": "https://example.org/post"},
            "preview": {"description": "Наш диван", "owner": "shop"},
        },
    }


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError("Unexpected integration request")


@asynccontextmanager
async def api(settings: Settings, handler=no_network):
    app = create_app(settings, transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


def make_draft(settings: Settings, source: str = "message") -> None:
    ingest_event(settings, event(source=source))
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("UPDATE jobs SET status = 'done'")
        connection.execute("INSERT INTO drafts(chat_id, message_id, text) VALUES (1, 1, 'Ответ')")
        save_lead(connection, 1, Lead(name="Анна", phone="+375290000000"))


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "settings", None),
        ("PUT", "settings", {"model": "other/model", "auto_reply": True}),
        ("GET", "chats", None),
        ("GET", "chats/1", None),
        ("GET", "status", None),
        ("POST", "chats/1/pause", {"paused": True}),
        ("POST", "chats/1/drafts/1/approve", None),
    ],
)
@pytest.mark.parametrize("token,code", [("synthetic-admin", 401), ("", 503)])
def test_admin_auth(
    settings: Settings, method: str, path: str, body: dict | None, token: str, code: int
) -> None:
    async def run():
        async with api(replace(settings, admin_token=token)) as client:
            response = await client.request(method, f"/admin/{path}", json=body)
            assert response.status_code == code

    asyncio.run(run())


def test_chat_lead_comment_and_pagination(settings: Settings) -> None:
    make_draft(settings, source="comment")
    ingest_event(settings, event("second", source="comment"))
    ingest_event(settings, event("third", source="comment"))

    async def run():
        async with api(settings) as client:
            listing = (await client.get("/admin/chats", headers=HEADERS)).json()
            assert listing["total"] == 1 and listing["items"][0]["name"] == "Анна"
            data = (await client.get("/admin/chats/1", headers=HEADERS)).json()
            assert data["lead"]["phone"] == "+375290000000"
            assert data["reply_block_reason"] == "comment_private_reply_unverified"
            assert data["message_total"] == 3 and len(data["messages"]) == 2
            assert data["messages"][0]["post_description"] == "Наш диван"
            assert data["drafts"][0]["status"] == "stale"
            older = (await client.get("/admin/chats/1?offset=2", headers=HEADERS)).json()
            assert len(older["messages"]) == 1 and older["messages"][0]["external_id"] == "first"
            assert (await client.get("/admin/chats/999", headers=HEADERS)).status_code == 404
            assert (await client.get("/admin/chats?offset=-1", headers=HEADERS)).status_code == 422

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        {"model": "", "auto_reply": False},
        {"model": "bad model", "auto_reply": True},
        {"model": "a" * 201, "auto_reply": False},
        {"model": "a", "auto_reply": "false"},
        {"model": "a", "auto_reply": 1},
        {"model": "a"},
        {"model": "a", "auto_reply": False, "token": "x"},
        [],
    ],
)
def test_invalid_settings_are_atomic(settings: Settings, body: object) -> None:
    async def run():
        async with api(settings) as client:
            before = (await client.get("/admin/settings", headers=HEADERS)).json()
            assert (
                await client.put("/admin/settings", json=body, headers=HEADERS)
            ).status_code == 400
            assert (await client.get("/admin/settings", headers=HEADERS)).json() == before

    asyncio.run(run())


def test_model_persists_and_next_generation_uses_it(settings: Settings) -> None:
    async def run():
        async with api(settings) as client:
            result = await client.put(
                "/admin/settings",
                headers=HEADERS,
                json={"model": " chosen/model ", "auto_reply": False},
            )
            assert result.json() == {"model": "chosen/model", "auto_reply": False}
        # Restart cannot reset the saved settings.
        async with api(settings) as client:
            assert (await client.get("/admin/settings", headers=HEADERS)).json()[
                "model"
            ] == "chosen/model"
        ingest_event(settings, event())

        def handler(request):
            assert json.loads(request.content)["model"] == "chosen/model"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"reply":"Что нужно?","lead":{}}'},
                        }
                    ]
                },
            )

        knowledge = KnowledgeBase(settings)
        async with (
            OpenRouterClient(
                replace(settings, openrouter_api_key="synthetic"),
                transport=httpx.MockTransport(handler),
            ) as router,
            UmnicoClient(settings, transport=httpx.MockTransport(no_network)) as umnico,
        ):
            assert await process_next_job(settings, knowledge, router, umnico)
        assert chat_detail(settings, 1)["drafts"][0]["status"] == "pending"

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["sent", "unknown", "failed"])
def test_approval_replay_and_late_receipt(settings: Settings, outcome: str) -> None:
    make_draft(settings)
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["customId"] == "draft:1"
        if outcome == "unknown":
            raise httpx.ReadTimeout("synthetic", request=request)
        return (
            httpx.Response(200, json=[{"messageId": "sent"}])
            if outcome == "sent"
            else httpx.Response(400)
        )

    async def run():
        async with api(settings, handler) as client:
            url = "/admin/chats/1/drafts/1/approve"
            first = await client.post(url, headers=HEADERS)
            assert first.status_code == 200 and first.json()["status"] == outcome
            assert (await client.post(url, headers=HEADERS)).json() == first.json()
            data = chat_detail(settings, 1)
            assert data["drafts"][0]["status"] == outcome and data["chat"]["paused"] == 0
            if outcome == "unknown":
                denied = await client.post(
                    "/admin/chats/1/send-text",
                    headers=HEADERS,
                    json={"request_id": "new", "text": "Ещё"},
                )
                assert denied.json()["error_code"] == "unresolved_send"
                receipt = event("sent")
                receipt["type"] = "message.outgoing"
                receipt["message"].update(incoming=False, customId="draft:1")
                receipt["message"]["message"]["text"] = "Ответ"
                ingest_event(settings, receipt)
                assert (await client.post(url, headers=HEADERS)).json()["status"] == "sent"
                assert chat_detail(settings, 1)["drafts"][0]["status"] == "sent"
            data = (await client.get("/admin/status", headers=HEADERS)).json()
            assert len(data["calls"]) == 1 and data["calls"][0]["operation"] == "send_text"
            assert (
                data["last_error"] is None if outcome == "sent" else data["last_error"] is not None
            )

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["new_message", "pause", "comment"])
def test_approval_refuses_ineligible_draft_without_network(settings: Settings, change: str) -> None:
    make_draft(settings, "comment" if change == "comment" else "message")
    if change == "new_message":
        ingest_event(settings, event("new"))
    if change == "pause":
        set_pause(settings, 1, True)

    async def run():
        async with api(settings) as client:
            response = await client.post("/admin/chats/1/drafts/1/approve", headers=HEADERS)
            if change == "comment":
                assert response.json()["error_code"] == "comment_private_reply_unverified"
                assert response.json()["status"] == "blocked"
            else:
                assert response.status_code == 409
            assert (
                await client.post("/admin/chats/2/drafts/1/approve", headers=HEADERS)
            ).status_code == 404

    asyncio.run(run())


def test_new_message_during_route_resolution_invalidates_approval(settings: Settings) -> None:
    make_draft(settings)
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("UPDATE message_context SET source_real_id = NULL")

    def handler(request):
        assert request.method == "GET"
        ingest_event(settings, event("new"))
        return httpx.Response(
            200, json=[{"id": "sender", "type": "message", "saId": 88, "realId": 255}]
        )

    async def run():
        async with api(settings, handler) as client:
            result = await client.post("/admin/chats/1/drafts/1/approve", headers=HEADERS)
            assert result.status_code == 409
            assert chat_detail(settings, 1)["attempts"] == []

    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause_resume", "manual"])
def test_pause_or_manual_answer_invalidates_generation(settings: Settings, action: str) -> None:
    ingest_event(settings, event())
    sent = []

    async def run():
        async with api(
            settings,
            lambda request: (
                sent.append(request) or httpx.Response(200, json=[{"messageId": "sent"}])
            ),
        ) as client:

            def handler(request):
                if action == "pause_resume":
                    set_pause(settings, 1, True)
                    set_pause(settings, 1, False)
                else:
                    # The async handler below performs the manual send during generation.
                    raise AssertionError
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": '{"reply":"Старый ответ","lead":{}}'},
                            }
                        ]
                    },
                )

            async def generation(request):
                if action == "manual":
                    response = await client.post(
                        "/admin/chats/1/send-text",
                        headers=HEADERS,
                        json={"request_id": "manual", "text": "Отвечает человек"},
                    )
                    assert response.json()["status"] == "sent"
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {"content": '{"reply":"Старый ответ","lead":{}}'},
                                }
                            ]
                        },
                    )
                return handler(request)

            async with (
                OpenRouterClient(
                    replace(settings, openrouter_api_key="synthetic"),
                    transport=httpx.MockTransport(generation),
                ) as router,
                UmnicoClient(settings, transport=httpx.MockTransport(no_network)) as umnico,
            ):
                assert await process_next_job(settings, KnowledgeBase(settings), router, umnico)
            data = chat_detail(settings, 1)
            assert data["drafts"] == [] and bool(data["chat"]["paused"]) == (action == "manual")

    asyncio.run(run())
    assert len(sent) == (1 if action == "manual" else 0)


def test_pause_api_validates_and_preserves_pending_draft(settings: Settings) -> None:
    make_draft(settings)

    async def run():
        async with api(settings) as client:
            url = "/admin/chats/1/pause"
            assert (
                await client.post(url, headers=HEADERS, json={"paused": "false"})
            ).status_code == 400
            assert (await client.post(url, headers=HEADERS, json={"paused": True})).json() == {
                "paused": True
            }
            assert (await client.post(url, headers=HEADERS, json={"paused": False})).json() == {
                "paused": False
            }
            assert chat_detail(settings, 1)["drafts"][0]["status"] == "pending"
            assert (
                await client.post("/admin/chats/999/pause", headers=HEADERS, json={"paused": True})
            ).status_code == 404

    asyncio.run(run())


def test_manual_cannot_claim_internal_request_namespace(settings: Settings) -> None:
    make_draft(settings)

    async def run():
        async with api(settings) as client:
            for request_id in ("draft:1", "consultation:1"):
                response = await client.post(
                    "/admin/chats/1/send-text",
                    headers=HEADERS,
                    json={"request_id": request_id, "text": "Ответ"},
                )
                assert response.status_code == 409
            assert chat_detail(settings, 1)["attempts"] == []

    asyncio.run(run())
