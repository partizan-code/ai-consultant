"""Administrative workflows on temporary SQLite, ASGI and mocked integrations only."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.admin import chat_detail, list_chats, set_pause
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


def test_instagram_profile_does_not_replace_lead_name(settings: Settings) -> None:
    make_draft(settings)
    with open_database(settings.database_path, timeout=1) as connection, connection:
        # A database from the previous version may contain an unused name cache.
        connection.execute(
            "CREATE TABLE chat_account_names (chat_id INTEGER PRIMARY KEY, account_name TEXT)"
        )
        connection.execute("INSERT INTO chat_account_names VALUES (1, 'Старое имя аккаунта')")
    enriched = event()
    enriched["message"]["sender"] = {"login": "anna.instagram", "type": "instagramV3"}
    assert ingest_event(settings, enriched) == "duplicate"

    async def run():
        async with api(settings) as client:
            listing = (await client.get("/admin/chats", headers=HEADERS)).json()["items"][0]
            assert listing["instagram_username"] == "anna.instagram"
            assert listing["name"] == "Анна"
            assert "account_name" not in listing
            detail = (await client.get("/admin/chats/1", headers=HEADERS)).json()
            assert detail["chat"]["instagram_username"] == "anna.instagram"
            assert detail["lead"]["name"] == "Анна"
            assert len(detail["drafts"]) == 1 and detail["drafts"][0]["status"] == "pending"

    asyncio.run(run())
    with open_database(settings.database_path, timeout=1) as connection:
        assert connection.execute("SELECT account_name FROM chat_account_names").fetchone()[0] == (
            "Старое имя аккаунта"
        )


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


@pytest.mark.parametrize("comment_first", [False, True])
def test_one_conversation_keeps_routes_and_sends_manual_reply_in_direct(
    settings: Settings, comment_first: bool
) -> None:
    first_source, second_source = (
        ("comment", "message") if comment_first else ("message", "comment")
    )
    make_draft(settings, first_source)
    second = event("second", second_source)
    second["message"]["source"].update(id="second-source", realId=256)
    second["message"]["datetime"] = "2026-11-20T10:01:00Z"
    second["message"]["sender"] = {"login": "customer", "type": "instagramV3"}
    second["message"]["preview"]["photo"] = "https://example.test/photo.jpg"
    ingest_event(settings, second)
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute(
            "INSERT INTO drafts(chat_id, message_id, text) VALUES (2, 2, 'Новый ответ')"
        )
        save_lead(connection, 2, Lead(city="Минск"))
    listing = list_chats(settings)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["id"] == 1 and item["instagram_username"] == "customer"
    assert item["pending_drafts"] == 1
    assert set(item["source_types"].split(",")) == {"message", "comment"}
    detail = chat_detail(settings, 1)
    assert detail == chat_detail(settings, 2)  # Old source links open the same conversation.
    assert detail["message_total"] == 2
    assert [row["external_id"] for row in detail["messages"]] == ["first", "second"]
    assert detail["messages"][1]["post_photo_url"] == "https://example.test/photo.jpg"
    assert detail["lead"]["name"] == "Анна" and detail["lead"]["city"] == "Минск"
    direct_id = 2 if comment_first else 1
    assert detail["reply_chat_id"] == direct_id and detail["reply_block_reason"] is None
    sent = []

    def handler(request):
        body = json.loads(request.content)
        assert body["source"] == (256 if comment_first else 255)
        sent.append(body)
        return httpx.Response(200, json=[{"messageId": "sent"}])

    async def run():
        async with api(settings, handler) as client:
            result = await client.post(
                f"/admin/chats/{direct_id}/send-text",
                headers=HEADERS,
                json={"request_id": "manual", "text": "Ответ в Direct"},
            )
            assert result.status_code == 200 and result.json()["status"] == "sent"

    asyncio.run(run())
    assert len(sent) == 1
    detail = chat_detail(settings, 1)
    assert detail["attempts"][0]["chat_id"] == direct_id
    assert all(draft["status"] == "stale" for draft in detail["drafts"])
    with open_database(settings.database_path, timeout=1) as connection:
        assert [row[0] for row in connection.execute("SELECT paused FROM chats")] == [1, 1]
        assert {row[0] for row in connection.execute("SELECT status FROM jobs")} == {"done"}


@pytest.mark.parametrize("boundary", ["accountId", "leadId", "integration"])
def test_same_login_does_not_merge_different_customers_or_integrations(
    settings: Settings, boundary: str
) -> None:
    first = event()
    first["message"]["sender"] = {"login": "same_login", "type": "instagramV3"}
    ingest_event(settings, first)
    second = event("second", "comment")
    second["message"]["sender"] = first["message"]["sender"]
    if boundary == "integration":
        second["message"]["sa"]["id"] = 99
    else:
        second[boundary] = 999
    ingest_event(settings, second)
    paged = replace(settings, panel_page_size=1)
    assert list_chats(paged)["total"] == 2
    assert len(list_chats(paged)["items"]) == len(list_chats(paged, 1)["items"]) == 1
    assert chat_detail(settings, 1)["message_total"] == 1
    assert chat_detail(settings, 2)["reply_chat_id"] is None


def test_conversation_pause_applies_to_existing_and_future_sources(settings: Settings) -> None:
    ingest_event(settings, event())
    set_pause(settings, 1, True)
    ingest_event(settings, event("comment", "comment"))
    with open_database(settings.database_path, timeout=1) as connection:
        assert [row[0] for row in connection.execute("SELECT paused FROM chats")] == [1, 1]
    set_pause(settings, 2, False)
    with open_database(settings.database_path, timeout=1) as connection:
        assert [row[0] for row in connection.execute("SELECT paused FROM chats")] == [0, 0]


def test_multiple_direct_routes_require_explicit_selection(settings: Settings) -> None:
    ingest_event(settings, event())
    second = event("second")
    second["message"]["source"]["id"] = "another-direct"
    ingest_event(settings, second)
    assert list_chats(settings)["total"] == 1
    detail = chat_detail(settings, 1)
    assert detail["reply_chat_id"] is None
    assert detail["reply_block_reason"] == "ambiguous_direct_route"


def test_import_enriches_old_comment_photo_in_combined_conversation(settings: Settings) -> None:
    direct = event()
    comment = event("comment", "comment")
    comment["message"]["source"].update(id="comment-source", realId=256)
    ingest_event(settings, direct)
    ingest_event(settings, comment)
    by_source = {255: direct["message"], 256: comment["message"]}
    for message in by_source.values():
        message["sentAt"] = message.pop("datetime")
    comment["message"]["preview"]["photo"] = "https://example.test/old-post.jpg"
    requested = []

    def handler(request):
        source = int(request.url.path.rsplit("/", 1)[1])
        requested.append(source)
        first = not json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "source": source if first else 0,
                "cursor": 1 if first else 0,
                "messages": [by_source[source]] if first else [],
            },
        )

    async def run():
        async with api(settings, handler) as client:
            response = await client.post("/admin/chats/1/import-history", headers=HEADERS)
            assert response.status_code == 200 and response.json() == {"imported": 0}

    asyncio.run(run())
    assert requested == [255, 255, 256, 256]
    detail = chat_detail(settings, 1)
    assert detail["message_total"] == 2
    assert detail["messages"][1]["post_photo_url"] == "https://example.test/old-post.jpg"
    with open_database(settings.database_path, timeout=1) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
