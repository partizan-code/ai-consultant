"""Proxy routing checks with the network connection boundary replaced before any I/O."""

import asyncio
from pathlib import Path

import httpx
import pytest

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.umnico import UmnicoClient


@pytest.fixture
def connection_attempts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    attempts = []

    async def connect_tcp(*, remote_host: str, remote_port: int, **kwargs) -> None:
        attempts.append((remote_host, remote_port))
        raise OSError("private-synthetic-connection-error")

    monkeypatch.setattr("anyio.connect_tcp", connect_tcp)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://unexpected.example:19090")
    monkeypatch.setenv("NO_PROXY", "*")
    return attempts


@pytest.mark.parametrize(
    "proxy_url,router_address",
    [
        ("", ("openrouter.ai", 443)),
        ("http://127.0.0.1:18080", ("127.0.0.1", 18080)),
        ("https://proxy.example:18443", ("proxy.example", 18443)),
    ],
)
def test_only_openrouter_uses_proxy_without_direct_fallback(
    proxy_url: str,
    router_address: tuple[str, int],
    connection_attempts: list[tuple[str, int]],
) -> None:
    settings = Settings(
        openrouter_api_key="synthetic-key",
        openrouter_proxy_url=proxy_url,
        umnico_api_key="synthetic-token",
    )

    async def run() -> None:
        async with OpenRouterClient(settings) as router, UmnicoClient(settings) as umnico:
            with pytest.raises(APIError, match="openrouter: transport_error") as error:
                await router.complete([{"role": "user", "content": "Synthetic"}])
            assert "private-" not in str(error.value)
            with pytest.raises(APIError, match="umnico: transport_error"):
                await umnico.list_integrations()
        assert router._http.is_closed and umnico._http.is_closed

    asyncio.run(run())
    assert connection_attempts == [router_address, ("api.umnico.com", 443)]


def test_injected_transport_keeps_control_with_proxy_configured(
    connection_attempts: list[tuple[str, int]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Synthetic"}, "finish_reason": "stop"}]},
        )

    async def run() -> None:
        async with OpenRouterClient(
            Settings(openrouter_api_key="synthetic", openrouter_proxy_url="http://proxy.example"),
            transport=httpx.MockTransport(handler),
        ) as router:
            assert await router.complete([{"role": "user", "content": "Synthetic"}]) == "Synthetic"

    asyncio.run(run())
    assert connection_attempts == []


@pytest.mark.parametrize("inject_transport", [False, True])
def test_staging_proxy_cannot_bypass_disabled_transport(
    tmp_path: Path,
    inject_transport: bool,
    connection_attempts: list[tuple[str, int]],
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Staging must not reach the injected transport")

    async def run() -> None:
        async with OpenRouterClient(
            Settings(
                environment="staging",
                database_path=tmp_path / "unused.sqlite3",
                knowledge_base_path=tmp_path / "knowledge",
                openrouter_api_key="synthetic",
                openrouter_proxy_url="http://proxy.example:18080",
            ),
            transport=httpx.MockTransport(forbidden) if inject_transport else None,
        ) as router:
            with pytest.raises(APIError, match="external_api_disabled"):
                await router.complete([{"role": "user", "content": "Synthetic"}])

    asyncio.run(run())
    assert connection_attempts == []
