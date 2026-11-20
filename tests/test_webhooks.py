"""Webhook registration contracts on MockTransport; no account or tunnel access."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.main import register_when_ready
from ai_consultant_bot.umnico import UmnicoClient
from ai_consultant_bot.webhooks import build_webhook_url

CALLBACK = "https://current.example.org/webhooks/umnico/synthetic-hook"


@pytest.mark.parametrize(
    "origin", ["https://current.example.org", " https://CURRENT.example.org:443/ "]
)
def test_build_callback(origin: str) -> None:
    assert build_webhook_url(origin, "synthetic-hook") == CALLBACK


@pytest.mark.parametrize(
    "origin",
    [
        "http://current.example.org",
        "https://user:pass@current.example.org",
        "https://current.example.org/path",
        "https://current.example.org/?a=b",
        "https://current.example.org/#fragment",
        "https://current.example.org/?",
        "https://localhost",
        "https://127.0.0.1",
        "https://192.168.1.1",
        "https://service.local",
        "https://[::1]",
        "https://cur\nrent.example.org",
    ],
)
def test_invalid_tunnel_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        build_webhook_url(origin, "synthetic-hook")


@pytest.mark.parametrize("token", ["", "has/slash", "has space", "has?query"])
def test_invalid_callback_token(token: str) -> None:
    with pytest.raises(ValueError):
        build_webhook_url("https://current.example.org", token)


@pytest.mark.parametrize("existing", [False, True])
def test_ensure_creates_once_or_reuses_url(existing: bool) -> None:
    calls = []
    record = {"id": 15, "url": CALLBACK, "status": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert request.url.path == "/v2/webhooks/"
        assert request.headers["Authorization"] == "JWT synthetic-key"
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        **record,
                        "url": "https://CURRENT.example.org:443/webhooks/umnico/synthetic-hook/",
                    }
                ]
                if existing or "POST" in calls
                else [],
            )
        assert json.loads(request.content) == {
            "url": CALLBACK,
            "name": "AI Consultant Bot",
            "events": [],
        }
        return httpx.Response(200, json=record)

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="synthetic-key"),
            transport=httpx.MockTransport(handler),
        ) as client:
            first, second = await asyncio.gather(
                client.ensure_webhook(CALLBACK),
                client.ensure_webhook(CALLBACK),
            )
            assert first.webhook_id == second.webhook_id == 15
            assert first.created is not existing
            assert not second.created

    asyncio.run(run())
    assert calls == (["GET", "GET"] if existing else ["GET", "POST", "GET"])


@pytest.mark.parametrize(
    "case,code",
    [
        ("disabled", "webhook_disabled"),
        ("limit", "webhook_limit_reached"),
        ("malformed", "invalid_webhook_response"),
        ("unauthorized", "http_error"),
    ],
)
def test_no_creation_after_failed_precheck(case: str, code: str) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert request.method == "GET"
        if case == "unauthorized":
            return httpx.Response(401, text="private-body")
        data = {
            "disabled": [{"id": 15, "url": CALLBACK, "status": 0}],
            "limit": [
                {"id": i, "url": f"https://old.example.org/{i}", "status": 1} for i in range(10)
            ],
            "malformed": [{"id": 15}],
        }[case]
        return httpx.Response(200, json=data)

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="synthetic-key"),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(APIError, match=code):
                await client.ensure_webhook(CALLBACK)

    asyncio.run(run())
    assert calls == ["GET"]


@pytest.mark.parametrize("case", ["timeout", "json", "mismatched_url", "missing_id", "bool_id"])
def test_creation_is_not_retried_on_unknown_result(case: str) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if case == "timeout":
            raise httpx.ReadTimeout("private-url", request=request)
        if case == "json":
            return httpx.Response(200, text="private-body")
        record = {"id": 15, "url": CALLBACK, "status": 1}
        if case == "mismatched_url":
            record["url"] = "https://other.example.org/webhook"
        elif case == "missing_id":
            del record["id"]
        else:
            record["id"] = True
        return httpx.Response(200, json=record)

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="synthetic-key"),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(APIError) as caught:
                await client.ensure_webhook(CALLBACK)
            assert "private" not in str(caught.value)
            assert "synthetic" not in str(caught.value)

    asyncio.run(run())
    assert calls == ["GET", "POST"]


def test_registration_waits_for_readiness_and_hides_callback(capsys: pytest.CaptureFixture) -> None:
    server = SimpleNamespace(started=False, should_exit=False)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert server.started
        calls.append(request.method)
        return httpx.Response(
            200,
            json=[]
            if request.method == "GET"
            else {
                "id": 15,
                "url": CALLBACK,
                "status": 1,
            },
        )

    async def run() -> None:
        task = asyncio.create_task(
            register_when_ready(
                server,
                Settings(umnico_api_key="synthetic-key"),
                CALLBACK,
                transport=httpx.MockTransport(handler),
            )
        )
        await asyncio.sleep(0)
        assert calls == []
        server.started = True
        await task

    asyncio.run(run())
    captured = capsys.readouterr()
    assert CALLBACK not in captured.out + captured.err
