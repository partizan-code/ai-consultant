"""HTTP helpers shared by the two integrations, without response-body logging."""

from __future__ import annotations

from typing import Any

import httpx


class APIError(Exception):
    """Safe error metadata: never include credentials, response bodies or URLs."""

    def __init__(self, service: str, code: str, status_code: int | None = None) -> None:
        self.service = service
        self.code = code
        self.status_code = status_code
        super().__init__(f"{service}: {code}" + (f" (HTTP {status_code})" if status_code else ""))


class DisabledAPITransport(httpx.AsyncBaseTransport):
    """Fail before any network access, including with accidentally copied credentials."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise APIError("integration", "external_api_disabled")


def create_http_client(
    base_url: str,
    *,
    connect_timeout: float,
    read_timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
    enabled: bool = True,
) -> httpx.AsyncClient:
    try:
        url = httpx.URL(base_url)
    except (httpx.InvalidURL, ValueError):
        raise ValueError("Invalid API base URL") from None
    if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
        raise ValueError("API base URL must be HTTPS without credentials, query or fragment")
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
        follow_redirects=False,
        trust_env=False,
        transport=transport if enabled else DisabledAPITransport(),
    )


async def request_json(
    client: httpx.AsyncClient,
    service: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Issue one request. No automatic retry, including after ambiguous timeouts."""
    try:
        response = await client.request(method, path, headers=headers, json=body)
    except httpx.TimeoutException:
        raise APIError(service, "timeout") from None
    except httpx.RequestError:
        raise APIError(service, "transport_error") from None
    if not response.is_success:
        raise APIError(service, "http_error", response.status_code)
    try:
        return response.json()
    except ValueError:
        raise APIError(service, "invalid_json", response.status_code) from None
