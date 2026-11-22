"""Explicit OpenRouter-only development chat; no backend, Umnico or database access."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from dotenv import dotenv_values
from loguru import logger

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.consultation import build_prompt, parse_consultation
from ai_consultant_bot.knowledge import KnowledgeBase, KnowledgeDocument
from ai_consultant_bot.logging_config import configure_logging
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.storage import Lead


def load_model_settings(path: Path, *, model: str | None = None) -> Settings:
    """Read only model settings from an explicit file, ignoring process credentials."""
    with path.open(encoding="utf-8") as stream:
        values = dotenv_values(stream=stream, interpolate=False)
    key = (values.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise ValueError("OPENROUTER_API_KEY is required")
    knowledge_path = Path(values.get("KNOWLEDGE_BASE_PATH") or "knowledge_base")
    if not knowledge_path.is_absolute():
        knowledge_path = path.resolve().parent / knowledge_path
    return Settings(
        environment="local",
        register_webhook=False,
        openrouter_api_key=key,
        openrouter_proxy_url=(values.get("OPENROUTER_PROXY_URL") or "").strip(),
        default_model=model or values.get("MODEL_TEST_MODEL") or Settings().default_model,
        knowledge_base_path=knowledge_path,
    )


class ModelDialogue:
    def __init__(
        self, settings: Settings, *, knowledge: Sequence[KnowledgeDocument] | None = None
    ) -> None:
        self.settings = settings
        self.knowledge = KnowledgeBase(settings)
        if knowledge is None:
            self.knowledge.reload()
        else:
            self.knowledge.documents = tuple(knowledge)
        self.history: list[dict[str, str]] = []
        self.lead = Lead()
        self.payment_options_mentioned = False

    def restore(self, state: object) -> None:
        """Restore bounded, plain session data without loading any client records."""
        if not isinstance(state, dict) or set(state) != {
            "history",
            "lead",
            "payment_options_mentioned",
        }:
            raise ValueError("Invalid test dialogue state")
        history, lead = state["history"], state["lead"]
        if (
            not isinstance(history, list)
            or len(history) > self.settings.history_context_messages + 1
            or not isinstance(lead, dict)
            or set(lead) != set(asdict(Lead()))
            or type(state["payment_options_mentioned"]) is not bool
        ):
            raise ValueError("Invalid test dialogue state")
        total = 0
        for item in history:
            if (
                not isinstance(item, dict)
                or set(item) != {"direction", "text"}
                or item["direction"] not in ("incoming", "outgoing")
                or not isinstance(item["text"], str)
            ):
                raise ValueError("Invalid test dialogue history")
            total += len(item["text"])
        if total > self.settings.history_context_chars + self.settings.consultation_reply_max_chars:
            raise ValueError("Test dialogue history exceeds context limit")
        if any(
            value is not None
            and (not isinstance(value, str) or len(value) > self.settings.lead_field_max_chars)
            for value in lead.values()
        ):
            raise ValueError("Invalid test dialogue lead")
        self.history = [dict(item) for item in history]
        self.lead = Lead(**lead)
        self.payment_options_mentioned = state["payment_options_mentioned"]

    def state(self) -> dict:
        return {
            "history": [dict(item) for item in self.history],
            "lead": asdict(self.lead),
            "payment_options_mentioned": self.payment_options_mentioned,
        }

    async def reply(self, text: str, client: OpenRouterClient) -> str:
        text = text.strip()
        if not text or len(text) > self.settings.history_context_chars:
            raise ValueError("Message is empty or exceeds the context limit")
        history = [*self.history, {"direction": "incoming", "text": text}]
        remaining = self.settings.history_context_chars
        context: list[dict[str, str]] = []
        for item in reversed(history[-self.settings.history_context_messages :]):
            if len(item["text"]) > remaining:
                break
            context.append(item)
            remaining -= len(item["text"])
        context.reverse()
        raw = await client.complete(
            build_prompt(
                history=context,
                lead=self.lead,
                post={},
                source_type="message",
                knowledge=self.knowledge.documents,
                dialogue_state={"payment_options_mentioned": self.payment_options_mentioned},
                reply_max_chars=self.settings.consultation_reply_max_chars,
                lead_field_max_chars=self.settings.lead_field_max_chars,
            ),
            model=self.settings.default_model,
        )
        result = parse_consultation(raw, context, self.settings, known_lead=self.lead)
        fields = asdict(self.lead)
        fields.update(
            {key: value for key, value in asdict(result.lead).items() if value is not None}
        )
        fields.update({key: None for key in result.cleared_fields})
        self.lead = Lead(**fields)
        self.history = [*context, {"direction": "outgoing", "text": result.reply}]
        self.payment_options_mentioned |= any(
            word in result.reply.casefold() for word in ("рассроч", "кредит", "частями")
        )
        return result.reply


async def run_dialogue(settings: Settings, messages: list[str]) -> None:
    dialogue = ModelDialogue(settings)
    async with OpenRouterClient(settings) as client:
        if messages:
            for text in messages:
                print(f"Клиент: {text}")
                print(f"Лейла: {await dialogue.reply(text, client)}")
            return
        print("Введите тестовое сообщение. /exit или Ctrl+C завершает диалог.")
        while True:
            text = input("Клиент: ").strip()
            if text == "/exit":
                return
            if text:
                print(f"Лейла: {await dialogue.reply(text, client)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.model-test"))
    parser.add_argument("--model", help="OpenRouter model ID; overrides MODEL_TEST_MODEL")
    parser.add_argument("--message", action="append", default=[], help="Repeat for a dialogue")
    args = parser.parse_args()
    configure_logging("INFO")
    try:
        settings = load_model_settings(args.env_file, model=args.model)
        print(f"Модель: {settings.default_model}")
        print("Реальные платные запросы к OpenRouter. Используйте вымышленные данные.")
        print("Umnico не подключён. Диалог хранится только в памяти этого процесса.")
        asyncio.run(run_dialogue(settings, args.message))
    except APIError as error:
        logger.error("Тест модели не выполнен: {}.", error)
        return 1
    except (OSError, ValueError):
        logger.error("Проверьте отдельный файл настроек, базу знаний и длину сообщения.")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nДиалог завершён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
