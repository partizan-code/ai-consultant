"""Startup orchestration with mocked settings, server and registration, without sockets."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

import ai_consultant_bot.main as startup
from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(umnico_api_key="synthetic-key", webhook_token="synthetic-hook")


def test_main_loads_settings_and_prompts_without_cli(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup, "load_settings", lambda: settings)
    values = iter(["http://bad.example.org", "https://current.example.org"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))
    monkeypatch.setattr("sys.argv", ["program", "--ignored-cli-argument"])
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(startup, "run_application", run)
    assert startup.main() == 0
    run.assert_awaited_once_with(settings, "https://current.example.org")


def test_main_accepts_programmatic_parameters(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(startup, "run_application", run)
    assert startup.main(settings, "https://current.example.org") == 0
    run.assert_awaited_once_with(settings, "https://current.example.org")


def test_main_rejects_missing_credentials_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Unexpected prompt"))
    assert startup.main(Settings()) == 1


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_prompt_can_be_cancelled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, error: type
) -> None:
    def cancel(prompt: str) -> str:
        raise error

    monkeypatch.setattr("builtins.input", cancel)
    assert startup.main(settings) == 0


@pytest.mark.parametrize("failure", [False, True])
def test_server_precedes_registration_and_stops_cleanly(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    servers = []

    class Server:
        def __init__(self, config: object) -> None:
            self.started = False
            self.should_exit = False
            self.closed = False
            servers.append(self)

        async def serve(self) -> None:
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)
            self.closed = True

    async def register(server: Server, current: Settings, url: str) -> None:
        assert server.started
        assert current is settings
        assert url == "https://current.example.org/webhooks/umnico/synthetic-hook"
        if failure:
            raise APIError("umnico", "timeout")
        server.should_exit = True

    monkeypatch.setattr(startup.uvicorn, "Server", Server)
    monkeypatch.setattr(startup, "register_when_ready", register)
    assert asyncio.run(startup.run_application(settings, "https://current.example.org")) == (
        1 if failure else 0
    )
    assert servers[0].closed


@pytest.mark.parametrize("error", [OSError, SystemExit])
def test_server_failure_cancels_registration(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error: type,
) -> None:
    class Server:
        started = False
        should_exit = False

        def __init__(self, config: object) -> None:
            pass

        async def serve(self) -> None:
            raise error("synthetic bind failure")

    monkeypatch.setattr(startup.uvicorn, "Server", Server)
    assert asyncio.run(startup.run_application(settings, "https://current.example.org")) == 1


def test_registration_wait_has_bounded_timeout(settings: Settings) -> None:
    from types import SimpleNamespace

    server = SimpleNamespace(started=False, should_exit=False)
    with pytest.raises(TimeoutError):
        asyncio.run(
            startup.register_when_ready(
                server,
                replace(settings, server_startup_timeout=0.001),
                "https://unused.example.org",
            )
        )
