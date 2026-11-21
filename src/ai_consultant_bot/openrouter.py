"""Minimal non-streaming OpenRouter chat client."""

from __future__ import annotations

from typing import Literal, TypedDict

import httpx

from ai_consultant_bot.api_http import APIError, create_http_client, request_json
from ai_consultant_bot.config import Settings


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class OpenRouterClient:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._http = create_http_client(
            settings.openrouter_base_url,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.openrouter_read_timeout,
            transport=transport,
            enabled=settings.external_apis_enabled,
            proxy_url=settings.openrouter_proxy_url or None,
        )

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> str:
        """Caller supplies a stored model override; secrets are never persisted here."""
        key = self._settings.openrouter_api_key
        if not key or any(char.isspace() for char in key):
            raise APIError("openrouter", "missing_or_invalid_api_key")
        selected_model = self._settings.default_model if model is None else model
        if not selected_model.strip() or not messages:
            raise ValueError("A model and at least one message are required")
        data = await request_json(
            self._http,
            "openrouter",
            "POST",
            "chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            body={
                "model": selected_model,
                "messages": messages,
                "max_tokens": self._settings.max_completion_tokens,
                "stream": False,
            },
        )
        try:
            if not isinstance(data, dict) or "error" in data:
                raise ValueError
            choice = data["choices"][0]
            text = choice["message"]["content"]
            if choice.get("finish_reason") != "stop":
                raise ValueError
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise APIError("openrouter", "invalid_completion_response") from None
        return text.strip()
