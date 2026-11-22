"""Pinned test chat API isolation; all HTTP is mocked and SQLite is temporary."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from ai_consultant_bot.config import Settings, load_settings
from ai_consultant_bot.model_sandbox_panel import empty_test_state
from ai_consultant_bot.storage import open_database
from ai_consultant_bot.web import create_app


def sandbox_settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        database_path=tmp_path / "test.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        admin_token="synthetic-admin",
        **kwargs,
    )


def completion(reply="Здравствуйте!", lead=None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"reply": reply, "lead": lead or {}}, ensure_ascii=False
                        ),
                    },
                }
            ]
        },
    )


def test_production_test_chat_uses_selected_model_and_loaded_knowledge_without_customer_rows(
    tmp_path,
) -> None:
    settings = sandbox_settings(
        tmp_path,
        environment="production",
        public_base_url="https://example.org",
        openrouter_api_key="synthetic-production-key",
        umnico_api_key="synthetic-umnico",
        model_test_env_file=tmp_path / "must-not-read.env",
    )
    settings.knowledge_base_path.mkdir()
    (settings.knowledge_base_path / "facts.md").write_text("Synthetic fact", encoding="utf-8")
    prompts = []

    def handler(request):
        assert request.url.host == "openrouter.ai", "Test chat must never call Umnico"
        assert request.headers["Authorization"] == "Bearer synthetic-production-key"
        prompts.append(json.loads(request.content))
        return completion("Чем помочь?", {"city": "Минск"})

    app = create_app(settings, transport=httpx.MockTransport(handler))
    headers = {"Authorization": "Bearer synthetic-admin"}

    async def run():
        async with app.router.lifespan_context(app):
            # The prompt must use the loaded snapshot, not silently reread files.
            (settings.knowledge_base_path / "facts.md").unlink()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://testserver",
            ) as client:
                body = {"text": "Я из Минск", "state": empty_test_state()}
                assert (await client.post("/admin/test-chat/reply", json=body)).status_code == 401
                await client.put(
                    "/admin/settings", headers=headers, json={"model": "selected/model"}
                )
                response = await client.post("/admin/test-chat/reply", headers=headers, json=body)
                assert response.status_code == 200
                state = response.json()["state"]
                assert state["lead"]["city"] == "Минск"
                response = await client.post(
                    "/admin/test-chat/reply",
                    headers=headers,
                    json={"text": "Нужен диван", "state": state},
                )
                assert response.status_code == 200
                # A fresh state starts a new conversation even in the same server process.
                response = await client.post("/admin/test-chat/reply", headers=headers, json=body)
                assert response.status_code == 200
            with open_database(
                settings.database_path, timeout=settings.database_timeout
            ) as connection:
                for table in ("chats", "messages", "leads", "jobs", "outgoing_attempts"):
                    assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    asyncio.run(run())
    assert len(prompts) == 3
    assert all(prompt["model"] == "selected/model" for prompt in prompts)
    assert (
        json.loads(prompts[0]["messages"][1]["content"])["knowledge"][0]["text"] == "Synthetic fact"
    )
    second = json.loads(prompts[1]["messages"][2]["content"])
    assert second["known_lead"]["city"] == "Минск" and len(second["history"]) == 3
    fresh = json.loads(prompts[2]["messages"][2]["content"])
    assert fresh["known_lead"]["city"] is None and len(fresh["history"]) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_staging_test_chat_requires_explicit_file_and_never_enables_umnico(
    tmp_path, enabled
) -> None:
    path = tmp_path / "model.env"
    path.write_text(
        "OPENROUTER_API_KEY=synthetic-test-key\nUMNICO_API_KEY=must-not-use\n",
        encoding="utf-8",
    )
    settings = sandbox_settings(
        tmp_path,
        environment="staging",
        openrouter_api_key="must-not-use",
        umnico_api_key="must-not-use",
        model_test_env_file=path if enabled else None,
    )
    requests = []

    def handler(request):
        assert enabled
        assert request.url.host == "openrouter.ai"
        assert request.headers["Authorization"] == "Bearer synthetic-test-key"
        requests.append(request)
        return completion()

    app = create_app(settings, transport=httpx.MockTransport(handler))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/admin/test-chat/reply",
                    headers={"Authorization": "Bearer synthetic-admin"},
                    json={"text": "Здравствуйте", "state": empty_test_state()},
                )
                assert response.status_code == (200 if enabled else 409)
                status = await client.get(
                    "/admin/status",
                    headers={"Authorization": "Bearer synthetic-admin"},
                )
                assert not status.json()["worker_enabled"]

    asyncio.run(run())
    assert len(requests) == int(enabled)


@pytest.mark.parametrize(
    "invalid",
    [
        {"text": "", "state": empty_test_state()},
        {"text": "x" * 16001, "state": empty_test_state()},
        {"text": "Привет", "state": {}},
        {"text": "Привет", "state": {**empty_test_state(), "history": "invalid"}},
        {"text": "Привет", "state": {**empty_test_state(), "lead": {"city": "x"}}},
        {"text": "Привет", "state": {**empty_test_state(), "payment_options_mentioned": "false"}},
    ],
)
def test_invalid_test_message_is_rejected_before_model_request(tmp_path, invalid) -> None:
    settings = sandbox_settings(tmp_path, openrouter_api_key="synthetic-key")

    def forbidden(request):
        pytest.fail("Invalid test requests must not call external APIs")

    app = create_app(settings, transport=httpx.MockTransport(forbidden))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/admin/test-chat/reply",
                    headers={"Authorization": "Bearer synthetic-admin"},
                    json=invalid,
                )
                assert response.status_code == 400

    asyncio.run(run())


def test_model_test_file_setting_is_explicit_and_can_be_cleared(tmp_path) -> None:
    path = tmp_path / "settings.env"
    model_path = tmp_path / "model.env"
    path.write_text(
        f"AI_CONSULTANT_MODEL_TEST_ENV_FILE={model_path.as_posix()}\n", encoding="utf-8"
    )
    settings = load_settings(path, environ={})
    assert settings.model_test_env_file == model_path
    assert replace(settings, model_test_env_file=None).model_test_env_file is None
    assert (
        load_settings(path, environ={"AI_CONSULTANT_MODEL_TEST_ENV_FILE": ""}).model_test_env_file
        is None
    )
