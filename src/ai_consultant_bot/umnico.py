"""Umnico client using the tested Direct contracts and bounded, single requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ai_consultant_bot.api_http import APIError, create_http_client, request_json
from ai_consultant_bot.config import Settings


def normalize_jwt(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("JWT must not contain line breaks")
    parts = value.strip().split()
    if len(parts) == 1 and parts[0].lower() not in {"jwt", "bearer"}:
        return f"JWT {parts[0]}"
    if len(parts) == 2 and parts[0].lower() == "jwt":
        return f"JWT {parts[1]}"
    raise ValueError("Expected a JWT token with an optional JWT prefix")


def messaging_id(value: str) -> str:
    """Internal lead/source IDs are positive integers, never arbitrary URL paths."""
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise APIError("umnico", "invalid_messaging_identifier")
    return str(int(value))


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class WebhookRegistration:
    webhook_id: int
    created: bool


def webhook_url_key(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise APIError("umnico", "invalid_webhook_response") from None
    return url.copy_with(path=url.path.rstrip("/"))


def validate_webhook(data: Any, expected_url: str) -> int:
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or data["id"] <= 0
        or not isinstance(data.get("url"), str)
        or webhook_url_key(data["url"]) != webhook_url_key(expected_url)
        or type(data.get("status")) is not int
        or data["status"] not in {0, 1}
    ):
        raise APIError("umnico", "invalid_webhook_response")
    if data["status"] == 0:
        raise APIError("umnico", "webhook_disabled")
    return data["id"]


def parse_token(payload: Any) -> AccessToken:
    try:
        if not isinstance(payload, dict):
            raise ValueError
        raw, expires = payload["token"], payload["expiresAt"]
        if not isinstance(raw, str) or not isinstance(expires, str):
            raise ValueError
        parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return AccessToken(normalize_jwt(raw), parsed)
    except (KeyError, ValueError):
        raise APIError("umnico", "invalid_token_response") from None


class UmnicoClient:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._access: AccessToken | None = None
        self._refresh: AccessToken | None = None
        self._auth_lock = asyncio.Lock()
        self._webhook_lock = asyncio.Lock()
        self._http = create_http_client(
            settings.umnico_base_url,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
            transport=transport,
        )

    async def __aenter__(self) -> UmnicoClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        data = await request_json(
            self._http,
            "umnico",
            "POST",
            "v2/auth/login",
            body={"login": self._settings.umnico_login, "pass": self._settings.umnico_password},
        )
        self._accept_tokens(data, require_refresh=True)

    def _accept_tokens(self, data: Any, *, require_refresh: bool = False) -> None:
        if not isinstance(data, dict) or "accessToken" not in data:
            raise APIError("umnico", "invalid_token_response")
        access = parse_token(data["accessToken"])
        refresh = self._refresh
        if "refreshToken" in data:
            refresh = parse_token(data["refreshToken"])
        elif require_refresh:
            raise APIError("umnico", "invalid_token_response")
        if access.expires_at <= datetime.now(UTC):
            raise APIError("umnico", "expired_token_response")
        self._access, self._refresh = access, refresh

    async def _authorization(self) -> str:
        if not self._settings.umnico_login:
            try:
                return normalize_jwt(self._settings.umnico_api_key)
            except ValueError:
                raise APIError("umnico", "missing_or_invalid_api_key") from None
        async with self._auth_lock:
            now = datetime.now(UTC)
            margin = timedelta(seconds=self._settings.token_refresh_margin)
            if self._access and self._access.expires_at > now + margin:
                return self._access.value
            if self._refresh and self._refresh.expires_at > now:
                try:
                    data = await request_json(
                        self._http,
                        "umnico",
                        "POST",
                        "v2/auth/tokens",
                        headers={"Authorization": self._refresh.value},
                    )
                except APIError as error:
                    if error.status_code != 401:
                        raise
                    await self._login()
                else:
                    self._accept_tokens(data)
            else:
                await self._login()
            assert self._access is not None
            return self._access.value

    async def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        authorization = await self._authorization()
        try:
            return await request_json(
                self._http,
                "umnico",
                method,
                path,
                headers={"Authorization": authorization},
                body=body,
            )
        except APIError as error:
            if error.status_code == 401 and self._access and self._access.value == authorization:
                self._access = None
            raise

    async def _get_list(self, path: str) -> list[dict[str, Any]]:
        data = await self._request("GET", path)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise APIError("umnico", "invalid_response")
        return data

    async def list_webhooks(self) -> list[dict[str, Any]]:
        return await self._get_list("v2/webhooks/")

    async def list_integrations(self) -> list[dict[str, Any]]:
        return await self._get_list("v2/integrations")

    async def list_managers(self) -> list[dict[str, Any]]:
        """Read employees; choosing a sender remains an explicit Settings decision."""
        return await self._get_list("v2/managers")

    async def list_threads(self, lead_id: str) -> list[dict[str, Any]]:
        data = await self._get_list(f"v2/messaging/{messaging_id(lead_id)}/threads")
        threads = []
        for item in data:
            if (
                not isinstance(item.get("id"), str)
                or not item["id"].strip()
                or not isinstance(item.get("type"), str)
                or type(item.get("saId")) is not int
                or item["saId"] <= 0
                or (
                    item.get("realId") is not None
                    and (type(item["realId"]) is not int or item["realId"] <= 0)
                )
            ):
                raise APIError("umnico", "invalid_threads_response")
            # In particular, source.token never leaves this method.
            threads.append(
                {key: item.get(key) for key in ("id", "realId", "type", "saId", "expiresAt")}
            )
        return threads

    async def ensure_webhook(self, url: str) -> WebhookRegistration:
        """Check before one POST; never change existing records or retry creation."""
        expected = webhook_url_key(url)
        if (
            expected.scheme != "https"
            or not expected.host
            or expected.userinfo
            or expected.query
            or expected.fragment
        ):
            raise ValueError("Webhook URL must be HTTPS without credentials, query or fragment")
        async with self._webhook_lock:
            webhooks = await self.list_webhooks()
            matches = []
            for webhook in webhooks:
                if not isinstance(webhook.get("url"), str):
                    raise APIError("umnico", "invalid_webhook_response")
                if webhook_url_key(webhook["url"]) == expected:
                    matches.append(webhook)
            if matches:
                active = next((item for item in matches if item.get("status") == 1), matches[0])
                return WebhookRegistration(validate_webhook(active, url), created=False)
            if len(webhooks) >= self._settings.webhook_limit:
                raise APIError("umnico", "webhook_limit_reached")
            data = await self._request(
                "POST",
                "v2/webhooks/",
                body={"url": url, "name": self._settings.webhook_name, "events": []},
            )
            return WebhookRegistration(validate_webhook(data, url), created=True)

    async def history_page(
        self, lead_id: str, source_real_id: str, *, cursor: int | None = None
    ) -> dict[str, Any]:
        if not self._settings.umnico_history_verified:
            raise APIError("umnico", "history_contract_unverified")
        lead_id, source_real_id = messaging_id(lead_id), messaging_id(source_real_id)
        if cursor is not None and (type(cursor) is not int or cursor <= 0):
            raise APIError("umnico", "invalid_history_cursor")
        data = await self._request(
            "POST",
            f"v2/messaging/{lead_id}/history/{source_real_id}",
            body={} if cursor is None else {"cursor": cursor},
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("messages"), list)
            or any(not isinstance(item, dict) for item in data["messages"])
            or type(data.get("cursor")) is not int
            or data["cursor"] < 0
            or type(data.get("source")) is not int
        ):
            raise APIError("umnico", "invalid_history_response")
        if not data["messages"]:
            if data["source"] != 0 or data["cursor"] != 0:
                raise APIError("umnico", "invalid_history_end")
        elif data["source"] != int(source_real_id):
            raise APIError("umnico", "history_source_mismatch")
        elif data["cursor"] == 0:
            raise APIError("umnico", "invalid_history_cursor")
        return data

    async def send_text(
        self,
        *,
        lead_id: str,
        source_real_id: str,
        integration_id: str,
        text: str,
        custom_id: str,
    ) -> None:
        if not self._settings.umnico_send_verified:
            raise APIError("umnico", "send_contract_unverified")
        lead_id, source_real_id = messaging_id(lead_id), messaging_id(source_real_id)
        try:
            user_id = int(self._settings.umnico_user_id)
            sa_id = int(integration_id)
            if user_id <= 0 or sa_id <= 0:
                raise ValueError
        except ValueError:
            raise APIError("umnico", "missing_or_invalid_sender") from None
        if not text.strip() or not custom_id.strip() or not source_real_id.strip():
            raise ValueError("Text, custom ID and source are required")
        data = await self._request(
            "POST",
            f"v2/messaging/{lead_id}/messages",
            body={
                "source": int(source_real_id),
                "userId": user_id,
                "saId": sa_id,
                "customId": custom_id,
                "message": {"text": text},
            },
        )
        if (
            not isinstance(data, list)
            or not data
            or any(not isinstance(item, dict) or not item for item in data)
        ):
            raise APIError("umnico", "invalid_send_response")
