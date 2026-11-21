"""End-to-end queue behavior with temporary SQLite and mocked HTTP only."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.admin import set_pause
from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.events import parse_event
from ai_consultant_bot.knowledge import KnowledgeBase
from ai_consultant_bot.messaging import ingest_event, send_text
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
    assert table in {"jobs", "suggestions", "leads", "chats", "api_calls", "outgoing_attempts"}
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
    suggestion = rows(settings, "suggestions")[0]
    assert suggestion["text"] == reply
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
    assert rows(settings, "suggestions")[0]["text"] == "У дивана Альфа обивка велюр."
    assert len(rows(settings, "jobs")) == 1
    assert rows(settings, "leads")[0]["budget"] == "2000"


@pytest.mark.parametrize("outcome", ["sent", "unknown", "failed"])
def test_auto_send_no_pause_and_no_retry(settings: Settings, outcome: str) -> None:
    ingest_event(settings, event("initial"))
    set_pause(settings, 1, False)
    ingest_event(settings, event())
    calls = []
    reply = "Анна, какие размеры дивана вам подойдут?"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("chat/completions"):
            return completion(reply, furniture_type="диван")
        assert request.url.path == "/v2/messaging/456/messages"
        assert json.loads(request.content)["customId"] == "consultation:2"
        assert json.loads(request.content)["message"]["text"] == reply
        if outcome == "unknown":
            raise httpx.ReadTimeout("private-timeout", request=request)
        return httpx.Response(400 if outcome == "failed" else 200, json=[{"messageId": "sent"}])

    asyncio.run(process(settings, handler))
    assert not asyncio.run(process(settings, handler))
    assert rows(settings, "chats")[0]["paused"] == 0
    assert rows(settings, "suggestions") == []
    assert rows(settings, "jobs")[-1]["status"] == ("unknown" if outcome == "unknown" else "done")
    assert len(calls) == 2
    if outcome == "unknown":
        receipt = event("sent", reply)
        receipt["type"] = "message.outgoing"
        receipt["message"].update(incoming=False, customId="consultation:2")
        ingest_event(settings, receipt)
        assert rows(settings, "jobs")[0]["status"] == "done"


def test_paused_comment_creates_suggestion_without_sending(settings: Settings) -> None:
    ingest_event(settings, event(source="comment"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("chat/completions")
        return completion()

    asyncio.run(process(settings, handler))
    assert rows(settings, "suggestions")[0]["text"]
    assert not rows(settings, "outgoing_attempts")


def test_comment_uses_direct_history_and_lead_without_auto_sending(settings: Settings) -> None:
    ingest_event(settings, event("direct", "Я Анна, нужен диван"))
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute("UPDATE jobs SET status = 'done'")
        connection.execute(
            "INSERT INTO suggestions(conversation_id, message_id, text) VALUES (1, 1, 'Старый')"
        )
        save_lead(connection, 1, Lead(name="Анна", city="Минск"))
    ingest_event(settings, event("comment", "А этот подойдёт?", source="comment"))
    assert rows(settings, "suggestions") == []

    def handler(request):
        assert request.url.path.endswith("chat/completions")
        content = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert [item["text"] for item in content["history"]] == [
            "Я Анна, нужен диван",
            "А этот подойдёт?",
        ]
        assert content["known_lead"]["name"] == "Анна"
        assert content["known_lead"]["city"] == "Минск"
        return completion()

    asyncio.run(process(settings, handler))
    assert len(rows(settings, "suggestions")) == 1
    assert rows(settings, "suggestions")[-1]["conversation_id"] == 1
    assert rows(settings, "suggestions")[-1]["message_id"] == 2
    assert not rows(settings, "outgoing_attempts")


def test_comment_during_direct_generation_invalidates_old_reply(settings: Settings) -> None:
    ingest_event(settings, event())

    def handler(request):
        assert request.url.path.endswith("chat/completions")
        ingest_event(settings, event("comment", "Уточнение", source="comment"))
        return completion()

    asyncio.run(process(settings, handler))
    assert not rows(settings, "suggestions") and not rows(settings, "outgoing_attempts")
    assert [row["status"] for row in rows(settings, "jobs")] == ["done", "pending"]


@pytest.mark.parametrize("change", ["incoming", "pause", "activate"])
def test_changes_during_generation_prevent_stale_save(settings: Settings, change: str) -> None:
    ingest_event(settings, event())
    if change == "pause":
        set_pause(settings, 1, False)
        ingest_event(settings, event("active"))

    def handler(request):
        assert request.url.path.endswith("chat/completions")
        if change == "incoming":
            ingest_event(settings, event("newer", "Нужна кровать"))
        else:
            set_pause(settings, 1, change == "pause")
        return completion(name="Анна")

    asyncio.run(process(settings, handler))
    assert not rows(settings, "outgoing_attempts")
    assert rows(settings, "suggestions") == rows(settings, "leads") == []
    if change == "pause":
        # Reusing the same source job cannot let its previous generation write a result.
        assert rows(settings, "jobs")[-1]["status"] == "pending"
        asyncio.run(process(settings, lambda request: completion("Новая подсказка")))
        assert rows(settings, "suggestions")[0]["text"] == "Новая подсказка"


def test_stale_queued_job_cancelled_and_old_suggestion_removed(settings: Settings) -> None:
    ingest_event(settings, event())
    asyncio.run(process(settings, lambda request: completion()))
    ingest_event(settings, event("second", "Кровать"))
    ingest_event(settings, event("third", "Минск"))
    assert rows(settings, "suggestions") == []

    assert rows(settings, "jobs")[1]["status"] == "done"
    asyncio.run(process(settings, lambda request: completion(city="Минск")))
    assert len(rows(settings, "suggestions")) == 1


@pytest.mark.parametrize("error", ["json", "lead", "http", "cancel"])
def test_generation_errors_do_not_save_lead_or_suggestion(settings: Settings, error: str) -> None:
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
    assert rows(settings, "suggestions") == rows(settings, "leads") == []
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
    initial = event("initial")
    initial["message"]["source"].pop("realId")
    ingest_event(settings, initial)
    set_pause(settings, 1, False)
    ingest_event(settings, incoming)

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
    assert rows(settings, "suggestions") == []


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
    ingest_event(settings, event("initial"))
    set_pause(settings, 1, False)
    ingest_event(settings, event())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("chat/completions"):
            return completion()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(process(settings, handler))
    assert rows(settings, "jobs")[-1]["status"] == "unknown"
    assert rows(settings, "suggestions") == []
    assert rows(settings, "outgoing_attempts")[0]["status"] == "unknown"


def test_each_paused_incoming_replaces_one_conversation_suggestion(settings: Settings) -> None:
    for index, source in enumerate(["message", "comment", "message"], 1):
        ingest_event(settings, event(str(index), source=source))
        assert rows(settings, "suggestions") == []
        asyncio.run(
            process(settings, lambda request, index=index: completion(f"Подсказка {index}"))
        )
        suggestions = rows(settings, "suggestions")
        assert len(suggestions) == 1
        assert suggestions[0]["conversation_id"] == 1
        assert suggestions[0]["message_id"] == index
        assert suggestions[0]["text"] == f"Подсказка {index}"
    assert not rows(settings, "outgoing_attempts")


def test_activation_discards_queued_work_until_next_incoming(settings: Settings) -> None:
    ingest_event(settings, event())
    set_pause(settings, 1, False)
    assert not asyncio.run(
        process(settings, lambda request: pytest.fail("Activation must not send"))
    )
    assert not rows(settings, "suggestions")
    # Active comments still extract a lead, but never send or expose a suggestion.
    ingest_event(settings, event("comment", source="comment"))
    asyncio.run(process(settings, lambda request: completion(name="Анна")))
    assert not rows(settings, "suggestions") and not rows(settings, "outgoing_attempts")
    assert rows(settings, "leads")[0]["name"] == "Анна"


def test_requeued_source_new_result_wins_over_late_old_generation(settings: Settings) -> None:
    ingest_event(settings, event())

    async def handler(request):
        set_pause(settings, 1, False)
        set_pause(settings, 1, True)
        assert await process(
            settings, lambda request: completion("Актуальная подсказка", city="Минск")
        )
        return completion("Устаревшая подсказка", name="Анна")

    asyncio.run(process(settings, handler))
    assert rows(settings, "suggestions")[0]["text"] == "Актуальная подсказка"
    assert rows(settings, "leads")[0]["name"] is None
    assert rows(settings, "jobs")[0]["status"] == "done"
    assert not rows(settings, "outgoing_attempts")


def test_manual_reply_consumes_source_until_new_incoming(settings: Settings) -> None:
    ingest_event(settings, event())
    asyncio.run(process(settings, lambda request: completion()))

    async def run():
        async with UmnicoClient(
            settings,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=[{"messageId": "sent"}])
            ),
        ) as client:
            await send_text(
                settings, client, chat_id=1, text="Ответ менеджера", request_id="manual"
            )
        assert not await process(settings, lambda request: pytest.fail("No new incoming"))
        set_pause(settings, 1, False)
        set_pause(settings, 1, True)
        assert not await process(settings, lambda request: pytest.fail("Already answered"))
        assert not rows(settings, "suggestions")
        ingest_event(settings, event("next"))
        await process(settings, lambda request: completion("Следующая подсказка"))

    asyncio.run(run())
    assert rows(settings, "chats")[0]["paused"] == 1
    assert rows(settings, "suggestions")[0]["message_id"] == 2
    assert len(rows(settings, "outgoing_attempts")) == 1


def test_paused_uncertain_send_allows_suggestion_but_not_another_send(settings: Settings) -> None:
    ingest_event(settings, event())
    with open_database(settings.database_path, timeout=1) as connection, connection:
        connection.execute(
            "INSERT INTO outgoing_attempts(request_id, chat_id, message_id, text, status) "
            "VALUES ('manual', 1, 1, 'Отправлен?', 'unknown')"
        )
    ingest_event(settings, event("next"))
    asyncio.run(process(settings, lambda request: completion("Новая подсказка")))
    assert rows(settings, "suggestions")[0]["message_id"] == 2
    assert len(rows(settings, "outgoing_attempts")) == 1


def test_pause_after_failed_auto_send_prepares_unanswered_suggestion(settings: Settings) -> None:
    ingest_event(settings, event("first"))
    set_pause(settings, 1, False)
    ingest_event(settings, event("second"))

    def handler(request):
        if request.url.path.endswith("chat/completions"):
            return completion()
        return httpx.Response(400)

    asyncio.run(process(settings, handler))
    set_pause(settings, 1, True)
    asyncio.run(process(settings, lambda request: completion("Подсказка после ошибки")))
    assert rows(settings, "suggestions")[0]["text"] == "Подсказка после ошибки"
    assert len(rows(settings, "outgoing_attempts")) == 1


def test_late_manual_receipt_does_not_remove_newer_suggestion(settings: Settings) -> None:
    ingest_event(settings, event())

    async def handler(request):
        ingest_event(settings, event("newer"))
        assert await process(settings, lambda request: completion("Ответ на новое сообщение"))
        receipt = event("sent", "Ответ на первое сообщение")
        receipt["type"] = "message.outgoing"
        receipt["message"].update(incoming=False, customId="manual")
        ingest_event(settings, receipt)
        return httpx.Response(200, json=[{"messageId": "sent"}])

    async def run():
        async with UmnicoClient(settings, transport=httpx.MockTransport(handler)) as client:
            await send_text(
                settings, client, chat_id=1, text="Ответ на первое сообщение", request_id="manual"
            )

    asyncio.run(run())
    assert rows(settings, "suggestions")[0]["message_id"] == 2
    assert rows(settings, "suggestions")[0]["text"] == "Ответ на новое сообщение"
    assert rows(settings, "outgoing_attempts")[0]["status"] == "sent"


def test_external_manager_reply_cancels_generation(settings: Settings) -> None:
    ingest_event(settings, event())

    def handler(request):
        outgoing = event("manager", "Ответ из Umnico")
        outgoing["type"] = "message.outgoing"
        outgoing["message"]["incoming"] = False
        ingest_event(settings, outgoing)
        return completion("Запоздавший текст")

    asyncio.run(process(settings, handler))
    assert not rows(settings, "suggestions") and not rows(settings, "leads")
    assert rows(settings, "chats")[0]["paused"] == 1
    assert not asyncio.run(process(settings, lambda request: pytest.fail("No unanswered input")))


def test_recovery_retries_requeued_suggestion_after_failed_send(settings: Settings) -> None:
    ingest_event(settings, event())
    set_pause(settings, 1, False)
    ingest_event(settings, event("second"))
    asyncio.run(
        process(
            settings,
            lambda request: (
                completion()
                if request.url.path.endswith("chat/completions")
                else httpx.Response(400)
            ),
        )
    )
    set_pause(settings, 1, True)
    with open_database(settings.database_path, timeout=1) as connection, connection:
        job = claim_next_job(connection)
        assert job["generation"] > 0
        assert recover_interrupted_jobs(connection) == 1
    asyncio.run(process(settings, lambda request: completion("Восстановленная подсказка")))
    assert rows(settings, "suggestions")[0]["text"] == "Восстановленная подсказка"
