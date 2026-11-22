"""Model sandbox isolation and conversation state, with synthetic keys and mocked HTTP."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.model_sandbox import ModelDialogue, load_model_settings
from ai_consultant_bot.openrouter import OpenRouterClient


def test_model_settings_ignore_backend_secrets_and_process_overrides(tmp_path, monkeypatch) -> None:
    path = tmp_path / "model.env"
    path.write_text(
        "OPENROUTER_API_KEY=synthetic-model-key\nAPP_ENV=production\n"
        "REGISTER_WEBHOOK=true\nUMNICO_LOGIN=private-login\nUMNICO_PASSWORD=private-password\n"
        "ADMIN_TOKEN=private-admin\nDATABASE_PATH=private.sqlite3\nMODEL_TEST_MODEL=test/model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "unexpected-process-key")
    monkeypatch.setenv("AI_CONSULTANT_ENV_FILE", str(tmp_path / "missing.env"))
    settings = load_model_settings(path)
    assert settings.environment == "local"
    assert not settings.register_webhook
    assert not any((settings.umnico_login, settings.umnico_password, settings.admin_token))
    assert settings.openrouter_api_key == "synthetic-model-key"
    assert settings.default_model == "test/model"
    assert settings.knowledge_base_path == tmp_path / "knowledge_base"
    assert load_model_settings(path, model="override/model").default_model == "override/model"


def test_missing_explicit_model_file_or_key_fails_closed(tmp_path) -> None:
    with pytest.raises(OSError):
        load_model_settings(tmp_path / "absent.env")
    path = tmp_path / "model.env"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_model_settings(path)


def test_dialogue_updates_and_clears_lead_without_database_or_umnico(tmp_path: Path) -> None:
    path = tmp_path / "model.env"
    path.write_text("OPENROUTER_API_KEY=synthetic-key\n", encoding="utf-8")
    settings = load_model_settings(path)
    dialogue = ModelDialogue(settings)
    payloads = []
    responses = iter(
        [
            {"reply": "Есть рассрочка. Какой диван нужен?", "lead": {"city": "Минск"}},
            {"reply": "Хорошо.", "lead": {"city": {"clear": "Забудьте город"}}},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai"
        assert request.url.path == "/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(next(responses), ensure_ascii=False),
                        },
                    }
                ]
            },
        )

    async def run() -> None:
        async with OpenRouterClient(settings, transport=httpx.MockTransport(handler)) as client:
            await dialogue.reply("Я из Минск", client)
            assert dialogue.lead.city == "Минск"
            await dialogue.reply("Забудьте город", client)
            assert dialogue.lead.city is None
            previous = list(dialogue.history)
            with pytest.raises(ValueError):
                await dialogue.reply("x" * (settings.history_context_chars + 1), client)
            assert dialogue.history == previous

    asyncio.run(run())
    second_context = json.loads(payloads[1]["messages"][2]["content"])
    assert second_context["known_lead"]["city"] == "Минск"
    assert second_context["dialogue_state"]["payment_options_mentioned"]
    assert second_context["history"][1]["direction"] == "outgoing"
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_model_response_does_not_advance_dialogue(tmp_path: Path) -> None:
    path = tmp_path / "model.env"
    path.write_text("OPENROUTER_API_KEY=synthetic-key\n", encoding="utf-8")
    settings = load_model_settings(path)
    dialogue = ModelDialogue(settings)

    async def run() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "invalid-json"}},
                    ]
                },
            )
        )
        async with OpenRouterClient(settings, transport=transport) as client:
            with pytest.raises(APIError, match="invalid_consultation_response"):
                await dialogue.reply("Здравствуйте", client)

    asyncio.run(run())
    assert dialogue.history == []
    assert dialogue.lead.city is None
