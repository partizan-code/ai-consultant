"""Offline step 2 contracts: temporary SQLite and synthetic HTTPX transports only."""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.events import history_timestamp, parse_event
from ai_consultant_bot.messaging import import_history, ingest_event, resolve_chat_route, send_text
from ai_consultant_bot.storage import claim_next_job, initialize_database, open_database
from ai_consultant_bot.umnico import UmnicoClient


def incoming(message_id: str = "new", source_type: str = "message") -> dict:
    return {
        "type": "message.incoming",
        "accountId": 123,
        "leadId": 456,
        "webhookId": "not-a-message-key",
        "message": {
            "messageId": message_id,
            "datetime": "2026-11-20T10:00:00Z",
            "incoming": True,
            "sa": {"id": 88, "type": "instagramV3"},
            "source": {
                "id": "source",
                "realId": 255,
                "type": source_type,
                "saId": 88,
                "sender": "sender",
                "token": "must-not-be-stored",
            },
            "message": {"text": "Нужен диван", "url": "https://example.org/post"},
        },
    }


def history_message(message_id: str = "old", *, incoming_direction: bool = True) -> dict:
    message = incoming(message_id)["message"]
    message["sentAt"] = message.pop("datetime")
    message["incoming"] = incoming_direction
    return message


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        umnico_api_key="synthetic-test",
        umnico_user_id="15",
        umnico_history_verified=True,
        umnico_send_verified=True,
    )
    with open_database(settings.database_path, timeout=1) as connection:
        initialize_database(connection, settings)
    return settings


def rows(settings: Settings, table: str) -> list[dict]:
    assert table in {
        "messages",
        "chats",
        "jobs",
        "message_context",
        "outgoing_attempts",
        "outgoing_receipts",
        "chat_profiles",
        "message_previews",
    }
    with open_database(settings.database_path, timeout=1) as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def test_routing_replay_and_ignored_lead_notifications(settings: Settings) -> None:
    event = incoming()
    assert ingest_event(settings, event) == "saved"
    replay = copy.deepcopy(event)
    replay["webhookId"] = "different-webhook"
    replay["leadId"] = 999  # A changed lead does not make a new message.
    assert ingest_event(settings, replay) == "duplicate"
    assert ingest_event(settings, {"type": "lead.changed", "lead": event["message"]}) == "ignored"
    assert ingest_event(settings, incoming("comment", "comment")) == "saved"
    assert len(rows(settings, "chats")) == 2
    assert rows(settings, "jobs") == []
    assert rows(settings, "message_context")[1]["reply_block_reason"] == (
        "comment_private_reply_unverified"
    )
    assert "must-not-be-stored" not in settings.database_path.read_bytes().decode(errors="ignore")


@pytest.mark.parametrize("preview", [None, {"description": "Описание", "owner": "owner"}])
def test_comment_without_preview_and_with_post_context(preview: dict | None) -> None:
    event = incoming(source_type="comment")
    event["message"]["preview"] = preview
    parsed = parse_event(event)
    assert parsed is not None
    assert parsed.text == "Нужен диван"
    assert parsed.post_url == "https://example.org/post"
    assert parsed.post_description == (preview["description"] if preview else None)


def test_photo_preview_is_retained_on_ingestion_history_and_replay(settings: Settings) -> None:
    event = incoming(source_type="comment")
    assert ingest_event(settings, event) == "saved"
    event["message"]["preview"] = {"photo": "https://example.test/post.jpg"}
    assert ingest_event(settings, event) == "duplicate"
    assert rows(settings, "message_previews") == [
        {"message_id": 1, "photo_url": "https://example.test/post.jpg"}
    ]
    event["message"]["preview"] = None
    assert ingest_event(settings, event) == "duplicate"
    assert len(rows(settings, "messages")) == 1
    assert rows(settings, "jobs") == []
    assert rows(settings, "message_previews")[0]["photo_url"].endswith("post.jpg")
    ingest_event(settings, incoming("direct"))
    old = history_message("old-photo")
    old["preview"] = {"photo": "https://example.test/history.jpg"}

    def handler(request):
        first = not json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "source": 255 if first else 0,
                "cursor": 1 if first else 0,
                "messages": [old] if first else [],
            },
        )

    async def run():
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            assert await import_history(settings, client, 2) == 1
            assert await import_history(settings, client, 2) == 0

    asyncio.run(run())
    assert rows(settings, "jobs") == []  # Import/replay never queues an extra reply.
    assert rows(settings, "message_previews")[-1]["photo_url"].endswith("history.jpg")


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        123,
        "",
        "C:/private/photo.jpg",
        "file:///etc/passwd",
        "//host/photo.jpg",
        "https://user:password@example.test/image",
        "https://:password@example.test/image",
        "https://[broken",
        "https://host/a\nb",
    ],
)
def test_invalid_photo_never_rejects_comment_or_loads_local_file(value: object) -> None:
    event = incoming(source_type="comment")
    event["message"]["preview"] = {"photo": value}
    parsed = parse_event(event)
    assert parsed is not None and parsed.post_photo_url is None


@pytest.mark.parametrize("source_type", ["message", "comment"])
def test_instagram_login_is_separate_from_business_and_post_names(
    settings: Settings, source_type: str
) -> None:
    event = incoming(source_type=source_type)
    event["message"]["sender"] = {"login": " @customer.name_1 ", "type": "instagramV3"}
    event["message"]["sa"]["login"] = "business_account"
    event["message"]["source"]["name"] = "Название поста"
    event["message"]["preview"] = {"owner": "post_owner"}
    assert ingest_event(settings, event) == "saved"
    assert rows(settings, "chat_profiles")[0]["instagram_username"] == "customer.name_1"


@pytest.mark.parametrize("login", [None, "", " ", "@", "not a login", "x" * 31, 123, {}])
def test_missing_or_invalid_instagram_login_does_not_reject_message(login: object) -> None:
    event = incoming()
    event["message"]["sender"] = {"login": login}
    parsed = parse_event(event)
    assert parsed is not None and parsed.instagram_username is None


def test_outgoing_and_other_channels_do_not_supply_instagram_login() -> None:
    event = incoming()
    event["message"]["sender"] = {"login": "business_account", "type": "instagramV3"}
    event["type"], event["message"]["incoming"] = "message.outgoing", False
    assert parse_event(event).instagram_username is None
    event["type"], event["message"]["incoming"] = "message.incoming", True
    event["message"]["sa"]["type"] = event["message"]["sender"]["type"] = "vkGroup"
    assert parse_event(event).instagram_username is None


def test_profile_enrichment_on_replay_preserves_route_and_latest_login(settings: Settings) -> None:
    event = incoming()
    assert ingest_event(settings, event) == "saved"
    event["message"]["sender"] = {"login": "customer"}
    assert ingest_event(settings, event) == "duplicate"
    assert rows(settings, "chat_profiles")[0]["instagram_username"] == "customer"

    replay = copy.deepcopy(event)
    replay["leadId"] = 999
    replay["message"]["sender"]["login"] = "wrong_route"
    assert ingest_event(settings, replay) == "duplicate"
    assert rows(settings, "chat_profiles")[0]["instagram_username"] == "customer"
    assert len(rows(settings, "chats")) == 1
    assert rows(settings, "jobs") == []

    renamed = copy.deepcopy(event)
    renamed["message"]["messageId"] = "renamed"
    renamed["message"]["datetime"] = "2026-11-20T14:00:00+03:00"
    renamed["message"]["sender"]["login"] = "renamed_customer"
    assert ingest_event(settings, renamed) == "saved"
    assert ingest_event(settings, event) == "duplicate"
    assert rows(settings, "chat_profiles")[0]["instagram_username"] == "renamed_customer"
    assert ingest_event(settings, incoming("without-profile")) == "saved"
    assert rows(settings, "chat_profiles")[0]["instagram_username"] == "renamed_customer"


@pytest.mark.parametrize(
    "field,value",
    [("incoming", False), ("datetime", "bad"), ("messageId", None), ("datetime", "2026-11-20")],
)
def test_invalid_message_rejected_without_writes(
    settings: Settings, field: str, value: object
) -> None:
    event = incoming()
    event["message"][field] = value
    with pytest.raises(ValueError):
        ingest_event(settings, event)
    assert rows(settings, "messages") == []


def test_outgoing_and_empty_text_do_not_create_jobs(settings: Settings) -> None:
    outgoing = incoming("outgoing")
    outgoing["type"], outgoing["message"]["incoming"] = "message.outgoing", False
    ingest_event(settings, outgoing)
    empty = incoming("attachment")
    empty["message"]["message"] = {}
    ingest_event(settings, empty)
    assert len(rows(settings, "messages")) == 2
    assert rows(settings, "jobs") == []


def test_paused_incoming_does_not_queue_generation(settings: Settings) -> None:
    ingest_event(settings, incoming())
    ingest_event(settings, incoming("second"))
    assert rows(settings, "jobs") == []
    with open_database(settings.database_path, timeout=1) as connection, connection:
        assert claim_next_job(connection) is None


@pytest.mark.parametrize("end_source,end_cursor", [(0, 0), (0, None), (255, None)])
def test_paginated_history_never_queues_replies_even_on_later_webhook(
    settings: Settings, end_source: int, end_cursor: int | None
) -> None:
    ingest_event(settings, incoming())
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.url.path == "/v2/messaging/456/history/255"
        page = (len(calls) - 1) % 3
        message = history_message(f"old-{page + 1}", incoming_direction=page == 0)
        return httpx.Response(
            200,
            json={
                "messages": [message] if page < 2 else [],
                "source": 255 if page < 2 else end_source,
                "cursor": 1795171785523 - page if page < 2 else end_cursor,
            },
        )

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            assert await import_history(settings, client, 1) == 2
            assert await import_history(settings, client, 1) == 0

    asyncio.run(run())
    sequence = [{}, {"cursor": 1795171785523}, {"cursor": 1795171785522}]
    assert calls == sequence * 2
    assert ingest_event(settings, incoming("old-1")) == "duplicate"
    assert rows(settings, "jobs") == []
    assert [row["imported"] for row in rows(settings, "messages")] == [0, 1, 1]


@pytest.mark.parametrize("source", [0, 255])
def test_empty_history_with_null_cursor_finishes_without_more_requests(
    settings: Settings, source: int
) -> None:
    ingest_event(settings, incoming())
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [], "source": source, "cursor": None})

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            assert await import_history(settings, client, 1) == 0

    asyncio.run(run())
    assert calls == [{}]
    assert len(rows(settings, "messages")) == 1
    assert rows(settings, "jobs") == []


def test_history_backfills_login_from_existing_messages_without_new_jobs(
    settings: Settings,
) -> None:
    ingest_event(settings, incoming())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/messaging/456/history/255"
        if json.loads(request.content).get("cursor"):
            return httpx.Response(200, json={"messages": [], "source": 0, "cursor": 0})
        message = history_message("new")
        message["sender"] = {"login": "customer_from_history"}
        outgoing = history_message("reply", incoming_direction=False)
        outgoing["sender"] = {"login": "business_account"}
        return httpx.Response(
            200, json={"messages": [message, outgoing], "source": 255, "cursor": 1795171785523}
        )

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            assert await import_history(settings, client, 1) == 1
            assert await import_history(settings, client, 1) == 0

    asyncio.run(run())
    assert rows(settings, "chat_profiles")[0]["instagram_username"] == "customer_from_history"
    assert len(rows(settings, "messages")) == 2 and not rows(settings, "jobs")


def test_old_database_adds_profiles_without_changing_saved_data(settings: Settings) -> None:
    ingest_event(settings, incoming())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("INSERT INTO leads(chat_id, name) VALUES (1, 'Анна')")
        connection.execute("UPDATE chats SET paused = 1")
        connection.execute("DROP TABLE chat_profiles")  # Synthetic pre-upgrade database only.
        initialize_database(connection, settings)
        initialize_database(connection, settings)
        assert connection.execute("SELECT name FROM leads").fetchone()[0] == "Анна"
        assert connection.execute("SELECT paused FROM chats").fetchone()[0] == 1
    assert len(rows(settings, "messages")) == 1
    assert rows(settings, "jobs") == []
    assert rows(settings, "chat_profiles") == []


@pytest.mark.parametrize("case", ["loop", "source", "shape", "limit"])
def test_history_fails_explicitly_on_bad_pages(settings: Settings, case: str) -> None:
    settings = replace(settings, history_max_pages=2)
    ingest_event(settings, incoming())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        message = history_message()
        if case == "source":
            message["sa"]["id"] = message["source"]["saId"] = 99
        if case == "shape":
            message["source"] = []
        return httpx.Response(
            200,
            json={
                "messages": [message],
                "source": 255,
                "cursor": calls if case == "limit" else 1,
            },
        )

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(APIError):
                await import_history(settings, client, 1)

    asyncio.run(run())
    assert calls <= 2
    assert rows(settings, "jobs") == []


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("success", "sent"),
        ("timeout", "unknown"),
        ("invalid_json", "unknown"),
        ("invalid_shape", "unknown"),
        ("empty_response", "unknown"),
        ("server_error", "unknown"),
        ("client_error", "failed"),
        ("request_timeout", "unknown"),
        ("cancel", "unknown"),
    ],
)
def test_send_once_and_persist_uncertainty(settings: Settings, outcome: str, expected: str) -> None:
    ingest_event(settings, incoming())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v2/messaging/456/messages"
        assert json.loads(request.content) == {
            "source": 255,
            "userId": 15,
            "saId": 88,
            "customId": "request-1",
            "message": {"text": "Здравствуйте"},
        }
        if outcome == "timeout":
            raise httpx.ReadTimeout("secret-not-for-logs", request=request)
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "invalid_json":
            return httpx.Response(200, text="private-body")
        return httpx.Response(
            {"server_error": 500, "client_error": 400, "request_timeout": 408}.get(outcome, 200),
            json=(
                {}
                if outcome == "invalid_shape"
                else []
                if outcome == "empty_response"
                else [{"messageId": "sent"}]
            ),
        )

    async def run() -> None:
        for _ in range(2):  # A fresh client also cannot resend the same request.
            async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
                try:
                    result = await send_text(
                        settings,
                        client,
                        chat_id=1,
                        text="Здравствуйте",
                        request_id="request-1",
                    )
                except asyncio.CancelledError:
                    assert outcome == "cancel"
                else:
                    assert result["status"] == expected
        if expected == "unknown":
            async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(APIError, match="unresolved_send"):
                    await send_text(settings, client, chat_id=1, text="new", request_id="new")

    asyncio.run(run())
    assert calls == 1
    assert rows(settings, "outgoing_attempts")[0]["status"] == expected
    assert rows(settings, "chats")[0]["paused"] == 1


@pytest.mark.parametrize(
    "source,verified,reason",
    [
        ("comment", True, "comment_private_reply_unverified"),
        ("message", False, "send_contract_unverified"),
    ],
)
def test_blocked_sends_never_touch_network(
    settings: Settings,
    source: str,
    verified: bool,
    reason: str,
) -> None:
    settings = replace(settings, umnico_send_verified=verified)
    ingest_event(settings, incoming(source_type=source))

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Blocked operations must not perform HTTP requests")

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await send_text(
                settings, client, chat_id=1, text="reply", request_id="blocked"
            )
            assert result["status"] == "blocked"
            assert result["error_code"] == reason

    asyncio.run(run())


def test_client_verification_gates_precede_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unverified contracts must not access the network, including login")

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_history_verified=False, umnico_send_verified=False),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(APIError, match="history_contract_unverified"):
                await client.history_page("456", "255")
            with pytest.raises(APIError, match="send_contract_unverified"):
                await client.send_text(
                    lead_id="456",
                    source_real_id="255",
                    integration_id="88",
                    text="test",
                    custom_id="test",
                )

    asyncio.run(run())


def test_concurrent_paused_webhook_delivery_deduplicates_without_jobs(settings: Settings) -> None:
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ingest_event(settings, incoming()), range(8)))
    assert results.count("saved") == 1
    assert results.count("duplicate") == 7
    assert rows(settings, "jobs") == []


def test_concurrent_sends_are_serialized_and_id_conflicts_rejected(settings: Settings) -> None:
    ingest_event(settings, incoming())
    calls = 0

    async def run() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json=[{"messageId": "sent"}])

        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            first = asyncio.create_task(
                send_text(
                    settings,
                    client,
                    chat_id=1,
                    text="first",
                    request_id="first",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            try:
                with pytest.raises(APIError, match="unresolved_send"):
                    await send_text(settings, client, chat_id=1, text="second", request_id="second")
                with pytest.raises(ValueError, match="different content"):
                    await send_text(settings, client, chat_id=1, text="changed", request_id="first")
            finally:
                release.set()
                await first

    asyncio.run(run())
    assert calls == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-20T10:00:00Z", "2026-11-20T10:00:00+00:00"),
        ("2026-11-20T13:00:00+03:00", "2026-11-20T10:00:00+00:00"),
        (1000, "1970-01-01T00:00:01+00:00"),
        ("1000", "1970-01-01T00:00:01+00:00"),
    ],
)
def test_history_time_formats(value: object, expected: str) -> None:
    assert history_timestamp(value) == expected


@pytest.mark.parametrize("value", [True, None, [], -1, "bad", "2026-11-20T10:00:00"])
def test_history_rejects_invalid_or_naive_time(value: object) -> None:
    with pytest.raises(ValueError):
        history_timestamp(value)


@pytest.mark.parametrize("missing_id", [False, True])
def test_history_validates_whole_page_before_writes(settings: Settings, missing_id: bool) -> None:
    ingest_event(settings, incoming())
    invalid = history_message("bad")
    if missing_id:
        del invalid["messageId"]
    else:
        invalid["sentAt"] = "invalid"
    payload = {"source": 255, "cursor": 10, "messages": [history_message("good"), invalid]}

    async def run() -> None:
        async with UmnicoClient(
            settings,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        ) as client:
            with pytest.raises(
                APIError, match="history_message_id_missing|invalid_history_message"
            ):
                await import_history(settings, client, 1)

    asyncio.run(run())
    assert len(rows(settings, "messages")) == 1
    assert rows(settings, "jobs") == []


def test_history_optional_source_fields_use_validated_route(settings: Settings) -> None:
    ingest_event(settings, incoming())
    old = history_message()
    old.pop("source")
    old.pop("sa")

    def handler(request: httpx.Request) -> httpx.Response:
        first = not json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "source": 255 if first else 0,
                "cursor": 10 if first else 0,
                "messages": [old] if first else [],
            },
        )

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            assert await import_history(settings, client, 1) == 1

    asyncio.run(run())
    assert len(rows(settings, "chats")) == 1
    assert rows(settings, "jobs") == []
    assert rows(settings, "message_context")[-1]["source_real_id"] == "255"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_resolve_missing_route_selects_matching_thread_only(
    settings: Settings, ambiguous: bool
) -> None:
    event = incoming()
    event["message"]["source"].pop("realId")
    ingest_event(settings, event)
    matching = {"id": "source", "type": "message", "saId": 88, "realId": 255}
    threads = [
        {**matching, "realId": None},
        {**matching, "saId": 99, "realId": 300},
        {**matching, "type": "comment", "realId": 301},
        {**matching, "id": "other", "realId": 302},
        matching,
    ]
    if ambiguous:
        threads.append({**matching, "realId": 303})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/messaging/456/threads"
        return httpx.Response(200, json=threads)

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            if ambiguous:
                with pytest.raises(APIError, match="ambiguous_source_real_id"):
                    await resolve_chat_route(settings, client, 1)
            else:
                assert (await resolve_chat_route(settings, client, 1))["source_real_id"] == "255"

    asyncio.run(run())


def outgoing(custom_id: str | None = "request-1") -> dict:
    event = incoming("outgoing")
    event["type"], event["message"]["incoming"] = "message.outgoing", False
    event["message"]["message"]["text"] = "Здравствуйте"
    if custom_id is not None:
        event["message"]["customId"] = custom_id
    return event


@pytest.mark.parametrize("envelope_id", [False, True])
def test_late_webhook_resolves_unknown_and_enriches_duplicate(
    settings: Settings,
    envelope_id: bool,
) -> None:
    ingest_event(settings, incoming())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            assert (
                await send_text(
                    settings, client, chat_id=1, text="Здравствуйте", request_id="request-1"
                )
            )["status"] == "unknown"
        assert ingest_event(settings, outgoing(None)) == "saved"
        assert rows(settings, "outgoing_attempts")[0]["status"] == "unknown"
        receipt = outgoing()
        if envelope_id:
            receipt["customId"] = receipt["message"].pop("customId")
        assert ingest_event(settings, receipt) == "duplicate"
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await send_text(
                settings, client, chat_id=1, text="Здравствуйте", request_id="request-1"
            )
            assert result["status"] == "sent" and result["error_code"] is None

    asyncio.run(run())
    assert calls == 1
    assert rows(settings, "jobs") == []
    assert rows(settings, "outgoing_receipts")[0]["custom_id"] == "request-1"


@pytest.mark.parametrize("outcome", ["success", "timeout", "invalid_json", "client_error"])
def test_webhook_before_http_result_is_never_downgraded(settings: Settings, outcome: str) -> None:
    ingest_event(settings, incoming())

    def handler(request: httpx.Request) -> httpx.Response:
        ingest_event(settings, outgoing())
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if outcome == "invalid_json":
            return httpx.Response(200, text="not JSON")
        return httpx.Response(
            400 if outcome == "client_error" else 200, json=[{"messageId": "outgoing"}]
        )

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await send_text(
                settings, client, chat_id=1, text="Здравствуйте", request_id="request-1"
            )
            assert result["status"] == "sent" and result["error_code"] is None

    asyncio.run(run())
    assert rows(settings, "outgoing_attempts")[0]["status"] == "sent"


@pytest.mark.parametrize("mismatch", ["account", "integration", "lead", "source", "text", "id"])
def test_receipt_cannot_confirm_different_chat_or_request(
    settings: Settings, mismatch: str
) -> None:
    ingest_event(settings, incoming())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute(
            "INSERT INTO outgoing_attempts(request_id, chat_id, text, status) "
            "VALUES ('request-1', 1, 'Здравствуйте', 'unknown')"
        )
    event = outgoing()
    if mismatch == "account":
        event["accountId"] = 999
    elif mismatch == "integration":
        event["message"]["sa"]["id"] = event["message"]["source"]["saId"] = 99
    elif mismatch == "lead":
        event["leadId"] = 999
    elif mismatch == "source":
        event["message"]["source"]["id"] = "other"
    elif mismatch == "text":
        event["message"]["message"]["text"] = "different"
    else:
        event["message"]["customId"] = "other"
    ingest_event(settings, event)
    assert rows(settings, "outgoing_attempts")[0]["status"] == "unknown"


def test_incoming_custom_id_does_not_confirm_send(settings: Settings) -> None:
    ingest_event(settings, incoming())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute(
            "INSERT INTO outgoing_attempts(request_id, chat_id, text, status) "
            "VALUES ('request-1', 1, 'Здравствуйте', 'unknown')"
        )
    event = incoming("another-incoming")
    event["customId"] = event["message"]["customId"] = "request-1"
    ingest_event(settings, event)
    assert rows(settings, "outgoing_receipts") == []
    assert rows(settings, "outgoing_attempts")[0]["status"] == "unknown"


def test_conflicting_receipt_is_rejected_without_overwrite(settings: Settings) -> None:
    ingest_event(settings, outgoing())
    with pytest.raises(ValueError, match="Conflicting custom ID"):
        ingest_event(settings, outgoing("different"))
    assert rows(settings, "outgoing_receipts")[0]["custom_id"] == "request-1"


@pytest.mark.parametrize("timeout", [False, True])
def test_send_replay_never_rediscovers_missing_route(settings: Settings, timeout: bool) -> None:
    event = incoming()
    event["message"]["source"].pop("realId")
    ingest_event(settings, event)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "source", "type": "message", "saId": 88, "realId": 255}]
            )
        if timeout:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200, json=[{"messageId": "sent"}])

    async def run() -> None:
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            first = await send_text(
                settings, client, chat_id=1, text="Здравствуйте", request_id="request-1"
            )
            second = await send_text(
                settings, client, chat_id=1, text="Здравствуйте", request_id="request-1"
            )
            assert first["status"] == second["status"] == ("unknown" if timeout else "sent")
            if timeout:
                with pytest.raises(APIError, match="unresolved_send"):
                    await send_text(settings, client, chat_id=1, text="new", request_id="new")

    asyncio.run(run())
    assert calls == ["/v2/messaging/456/threads", "/v2/messaging/456/messages"]


def test_old_database_adds_receipts_without_changing_saved_data(settings: Settings) -> None:
    ingest_event(settings, incoming())
    with open_database(settings.database_path, timeout=1) as connection:
        connection.execute("DROP TABLE outgoing_receipts")  # Synthetic pre-upgrade schema only.
        initialize_database(connection, settings)
        initialize_database(connection, settings)
    assert len(rows(settings, "messages")) == 1
    assert rows(settings, "jobs") == []
    assert rows(settings, "outgoing_receipts") == []
