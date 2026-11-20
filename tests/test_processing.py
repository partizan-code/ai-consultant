"""End-to-end queue behavior with temporary SQLite and mocked HTTP only."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.events import parse_event
from ai_consultant_bot.knowledge import KnowledgeBase
from ai_consultant_bot.messaging import ingest_event
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.processing import process_next_job
from ai_consultant_bot.storage import (
    Lead,
    claim_next_job,
    initialize_database,
    open_database,
    recover_interrupted_jobs,
    save_lead,
    set_setting,
    store_event,
)
from ai_consultant_bot.umnico import UmnicoClient


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        openrouter_api_key="synthetic-openrouter",
        umnico_api_key="synthetic-umnico",
        umnico_user_id="15",
    )
    with open_database(settings.database_path, timeout=1) as connection:
        initialize_database(connection, settings)
    return settings


def event(
    message_id: str = "new", text: str = "Нужен диван, я Анна, Минск", source: str = "message"
) -> dict:
    return {
        "type": "message.incoming",
        "accountId": 123,
        "leadId": 456,
        "message": {
            "messageId": message_id,
            "datetime": "2026-11-20T10:00:00Z",
            "incoming": True,
            "sa": {"id": 88},
            "source": {"id": "source", "realId": 255, "type": source},
            "message": {"text": text},
        },
    }


def rows(settings: Settings, table: str) -> list[dict]:
    assert table in {"jobs", "drafts", "leads", "chats", "api_calls", "outgoing_attempts"}
    with open_database(settings.database_path, timeout=1) as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]


def completion(
    reply: str = "Здравствуйте! Какие размеры дивана вам нужны?", **lead: str | None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"reply": reply, "lead": lead})},
                }
            ]
        },
    )


async def process(settings: Settings, handler, knowledge: KnowledgeBase | None = None) -> bool:
    if knowledge is None:
        knowledge = KnowledgeBase(settings)
        knowledge.reload()
    async with (
        OpenRouterClient(settings, transport=httpx.MockTransport(handler)) as router,
        UmnicoClient(settings, transport=httpx.MockTransport(handler)) as umnico,
    ):
        return await process_next_job(settings, knowledge, router, umnico)


@pytest.mark.parametrize("knowledge_state", ["missing", "empty", "unrelated"])
def test_model_reply_and_partial_lead_without_matching_facts(
    settings: Settings, knowledge_state: str
) -> None:
    if knowledge_state != "missing":
        settings.knowledge_base_path.mkdir()
    if knowledge_state == "unrelated":
        (settings.knowledge_base_path / "care.txt").write_text(
            "Уход: сухая чистка.", encoding="utf-8"
        )
    ingest_event(settings, event())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        save_lead(connection, 1, Lead(phone="+375290000000"))
        set_setting(connection, "model", "chosen/model")
    calls = []
    reply = "Анна, здравствуйте! Какие размеры дивана и бюджет вы рассматриваете?"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/chat/completions"
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "chosen/model"
        payload = json.loads(body["messages"][1]["content"])
        assert payload["knowledge"] == []
        assert payload["known_lead"]["phone"] == "+375290000000"
        return completion(reply, name="Анна", city="Минск", furniture_type="диван", phone=None)

    assert asyncio.run(process(settings, handler))
    assert not asyncio.run(process(settings, handler))
    assert ingest_event(settings, event()) == "duplicate"
    assert len(calls) == 1
    lead = rows(settings, "leads")[0]
    assert lead["name"] == "Анна" and lead["city"] == "Минск"
    assert lead["phone"] == "+375290000000" and lead["budget"] is None
    draft = rows(settings, "drafts")[0]
    assert draft["status"] == "pending" and draft["text"] == reply
    assert rows(settings, "jobs")[0]["status"] == "done"
    assert rows(settings, "outgoing_attempts") == []
    assert rows(settings, "api_calls")[0]["service"] == "openrouter"


def test_matching_knowledge_and_imported_history_are_context_only(settings: Settings) -> None:
    ingest_event(settings, event())
    old = event("old", "Мой бюджет 2000")
    old["message"]["datetime"] = "2026-11-19T10:00:00Z"
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        store_event(connection, parse_event(old), imported=True)
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "sofa.md").write_text("Диван Альфа: велюр.", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["history"][0]["text"] == "Мой бюджет 2000"
        assert data["knowledge"][0]["text"] == "Диван Альфа: велюр."
        return completion("У дивана Альфа обивка велюр.", budget="2000")

    asyncio.run(process(settings, handler))
    assert rows(settings, "drafts")[0]["text"] == "У дивана Альфа обивка велюр."
    assert len(rows(settings, "jobs")) == 1
    assert rows(settings, "leads")[0]["budget"] == "2000"


@pytest.mark.parametrize("outcome", ["sent", "unknown", "failed"])
def test_auto_send_no_pause_and_no_retry(settings: Settings, outcome: str) -> None:
    settings = replace(settings, auto_reply=True)
    with open_database(settings.database_path, timeout=1) as connection, connection:
        set_setting(connection, "auto_reply", "1")
    ingest_event(settings, event())
    calls = []
    reply = "Анна, какие размеры дивана вам подойдут?"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("chat/completions"):
            return completion(reply, furniture_type="диван")
        assert request.url.path == "/v2/messaging/456/messages"
        assert json.loads(request.content)["customId"] == "consultation:1"
        assert json.loads(request.content)["message"]["text"] == reply
        if outcome == "unknown":
            raise httpx.ReadTimeout("private-timeout", request=request)
        return httpx.Response(400 if outcome == "failed" else 200, json=[{"messageId": "sent"}])

    asyncio.run(process(settings, handler))
    assert not asyncio.run(process(settings, handler))
    assert rows(settings, "chats")[0]["paused"] == 0
    assert rows(settings, "drafts")[0]["status"] == outcome
    assert rows(settings, "drafts")[0]["text"] == reply
    assert rows(settings, "jobs")[0]["status"] == ("unknown" if outcome == "unknown" else "done")
    assert len(calls) == 2
    if outcome == "unknown":
        receipt = event("sent", rows(settings, "drafts")[0]["text"])
        receipt["type"] = "message.outgoing"
        receipt["message"].update(incoming=False, customId="consultation:1")
        ingest_event(settings, receipt)
        assert rows(settings, "jobs")[0]["status"] == "done"
        assert rows(settings, "drafts")[0]["status"] == "sent"


def test_comment_stays_private_draft_even_in_auto_mode(settings: Settings) -> None:
    with open_database(settings.database_path, timeout=1) as connection, connection:
        set_setting(connection, "auto_reply", "1")
    ingest_event(settings, event(source="comment"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("chat/completions")
        return completion()

    asyncio.run(process(settings, handler))
    assert rows(settings, "drafts")[0]["status"] == "pending"
    assert not rows(settings, "outgoing_attempts")


@pytest.mark.parametrize("change", ["incoming", "pause", "mode"])
def test_changes_during_generation_prevent_send(settings: Settings, change: str) -> None:
    ingest_event(settings, event())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        set_setting(connection, "auto_reply", "1")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("chat/completions")
        # Another processor cannot claim this chat while the first owns it.
        with open_database(settings.database_path, timeout=1) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            assert claim_next_job(connection) is None
            if change == "pause":
                connection.execute("UPDATE chats SET paused = 1")
            if change == "mode":
                set_setting(connection, "auto_reply", "0")
        if change == "incoming":
            ingest_event(settings, event("newer", "Нужна кровать"))
        return completion(name="Анна")

    asyncio.run(process(settings, handler))
    assert not rows(settings, "outgoing_attempts")
    assert rows(settings, "jobs")[0]["status"] == "done"
    if change == "mode":
        assert rows(settings, "drafts")[0]["status"] == "pending"
    else:
        assert rows(settings, "drafts") == rows(settings, "leads") == []


def test_stale_queued_job_skipped_and_old_draft_invalidated(settings: Settings) -> None:
    ingest_event(settings, event())
    asyncio.run(process(settings, lambda request: completion()))
    ingest_event(settings, event("second", "Кровать"))
    ingest_event(settings, event("third", "Минск"))
    assert rows(settings, "drafts")[0]["status"] == "stale"

    def no_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("Superseded queued job must be skipped before HTTP")

    asyncio.run(process(settings, no_request))
    assert rows(settings, "jobs")[1]["status"] == "done"
    asyncio.run(process(settings, lambda request: completion(city="Минск")))
    assert rows(settings, "drafts")[-1]["status"] == "pending"


@pytest.mark.parametrize("error", ["json", "lead", "http", "cancel"])
def test_generation_errors_do_not_save_lead_or_draft(settings: Settings, error: str) -> None:
    ingest_event(settings, event())

    def handler(request: httpx.Request) -> httpx.Response:
        if error == "cancel":
            raise asyncio.CancelledError
        if error == "json":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "not JSON"},
                        }
                    ]
                },
            )
        if error == "lead":
            return completion(city="Москва")
        return httpx.Response(500, text="private-body")

    with pytest.raises(asyncio.CancelledError if error == "cancel" else APIError):
        asyncio.run(process(settings, handler))
    assert rows(settings, "jobs")[0]["status"] == "failed"
    assert rows(settings, "drafts") == rows(settings, "leads") == []
    assert rows(settings, "api_calls")[0]["error_code"]


@pytest.mark.parametrize("state", [None, "sent", "unknown", "failed"])
def test_restart_retries_generation_but_never_uncertain_send(
    settings: Settings, state: str | None
) -> None:
    ingest_event(settings, event())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("UPDATE jobs SET status = 'processing'")
        if state:
            connection.execute(
                "INSERT INTO outgoing_attempts(request_id, chat_id, text, status) VALUES (?, 1, 'text', ?)",
                ("consultation:1", state),
            )
        assert recover_interrupted_jobs(connection) == 1
    expected = "pending" if state is None else "unknown" if state == "unknown" else "done"
    assert rows(settings, "jobs")[0]["status"] == expected


def test_new_message_during_route_lookup_blocks_stale_auto_send(settings: Settings) -> None:
    incoming = event()
    incoming["message"]["source"].pop("realId")
    ingest_event(settings, incoming)
    with open_database(settings.database_path, timeout=1) as connection, connection:
        set_setting(connection, "auto_reply", "1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("chat/completions"):
            return completion()
        assert request.method == "GET" and request.url.path.endswith("/threads")
        ingest_event(settings, event("newer"))
        return httpx.Response(
            200, json=[{"id": "source", "type": "message", "saId": 88, "realId": 255}]
        )

    asyncio.run(process(settings, handler))
    assert not rows(settings, "outgoing_attempts")
    assert rows(settings, "drafts")[0]["status"] == "stale"


def test_trigger_is_retained_in_bounded_history(settings: Settings) -> None:
    settings = replace(settings, history_context_messages=1, history_context_chars=12)
    ingest_event(settings, event(text="Диван Минск"))
    future = event("imported", "long imported message that must not replace the trigger")
    future["message"]["datetime"] = "2026-11-21T10:00:00Z"
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        store_event(connection, parse_event(future), imported=True)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert payload["history"] == [{"direction": "incoming", "text": "Диван Минск"}]
        return completion(city="Минск")

    asyncio.run(process(settings, handler))


def test_comment_post_is_used_for_search_not_lead_evidence(settings: Settings) -> None:
    comment = event(text="Расскажите подробнее", source="comment")
    comment["message"]["preview"] = {"description": "Диван Альфа", "owner": "shop"}
    ingest_event(settings, comment)
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "sofas.txt").write_text("Диван Альфа: велюр.", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["knowledge"][0]["text"] == "Диван Альфа: велюр."
        assert data["post"]["post_description"] == "Диван Альфа"
        return completion("Диван Альфа в велюре.", furniture_type="диван")

    with pytest.raises(APIError, match="invalid_consultation_response"):
        asyncio.run(process(settings, handler))
    assert not rows(settings, "leads")


def test_cancelled_auto_send_remains_unknown(settings: Settings) -> None:
    ingest_event(settings, event())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        set_setting(connection, "auto_reply", "1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("chat/completions"):
            return completion()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(process(settings, handler))
    assert rows(settings, "jobs")[0]["status"] == "unknown"
    assert rows(settings, "drafts")[0]["status"] == "unknown"
    assert rows(settings, "outgoing_attempts")[0]["status"] == "unknown"
