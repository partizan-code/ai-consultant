"""Integration client contracts. Every HTTP call uses MockTransport."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from loguru import logger

from ai_consultant_bot.api_http import APIError
from ai_consultant_bot.config import Settings
from ai_consultant_bot.openrouter import OpenRouterClient
from ai_consultant_bot.umnico import UmnicoClient, normalize_jwt


def tokens(access: str, seconds: int = 3600, *, refresh: bool = True) -> dict:
    data = {
        "accessToken": {
            "token": access,
            "expiresAt": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
        }
    }
    if refresh:
        data["refreshToken"] = {
            "token": "refresh-test",
            "expiresAt": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }
    return data


@pytest.mark.parametrize("raw", ["test-token", "JWT test-token", " jwt test-token "])
def test_jwt_prefix(raw: str) -> None:
    assert normalize_jwt(raw) == "JWT test-token"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "JWT",
        "Bearer test-token",
        "JWT JWT test-token",
        "JWT\ntest-token",
    ],
)
def test_invalid_jwt(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_jwt(raw)


def test_static_jwt_and_confirmed_get_paths() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "JWT test-token"
        assert request.extensions["timeout"]["connect"] == 5
        assert request.extensions["timeout"]["read"] == 20
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="test-token"), transport=httpx.MockTransport(handler)
        ) as client:
            assert await client.list_webhooks() == []
            assert await client.list_integrations() == []
        assert client._http.is_closed

    asyncio.run(run())
    assert [request.url.path for request in requests] == ["/v2/webhooks/", "/v2/integrations"]


def test_login_cached_across_concurrent_requests() -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v2/auth/login":
            assert "Authorization" not in request.headers
            assert json.loads(request.content) == {"login": "test", "pass": " password "}
            return httpx.Response(200, json=tokens("login-test"))
        assert request.headers["Authorization"] == "JWT login-test"
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_login="test", umnico_password=" password ", umnico_api_key="ignored"),
            transport=httpx.MockTransport(handler),
        ) as client:
            await asyncio.gather(client.list_integrations(), client.list_integrations())

    asyncio.run(run())
    assert paths.count("/v2/auth/login") == 1


@pytest.mark.parametrize("refresh_rejected", [False, True])
def test_refresh_keeps_previous_refresh_or_logs_in_after_401(refresh_rejected: bool) -> None:
    paths = []
    login_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        paths.append(request.url.path)
        if request.url.path == "/v2/auth/login":
            login_count += 1
            return httpx.Response(200, json=tokens(f"login-{login_count}", 20))
        if request.url.path == "/v2/auth/tokens":
            assert request.headers["Authorization"] == "JWT refresh-test"
            assert request.content == b""
            if refresh_rejected:
                return httpx.Response(401)
            return httpx.Response(200, json=tokens("refreshed", 20, refresh=False))
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_login="test", umnico_password="test"),
            transport=httpx.MockTransport(handler),
        ) as client:
            for _ in range(3):
                await client.list_webhooks()

    asyncio.run(run())
    assert paths.count("/v2/auth/tokens") == 2
    assert login_count == (3 if refresh_rejected else 1)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json=["invalid"]),
        httpx.Response(200, text="private-body"),
        httpx.Response(401, text="private-body"),
        httpx.Response(302, headers={"Location": "https://example.org/redirect"}),
    ],
)
def test_errors_are_safe_and_not_retried_or_redirected(response: httpx.Response) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="private-token"), transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(APIError) as error:
                await client.list_webhooks()
            assert "private" not in str(error.value)

    asyncio.run(run())
    assert calls == 1


def test_openrouter_model_override_and_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "selected/model",
            "messages": [{"role": "user", "content": "Тест"}],
            "max_tokens": 1000,
            "stream": False,
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": " Ответ "},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async def run() -> None:
        async with OpenRouterClient(
            Settings(openrouter_api_key="test-key"), transport=httpx.MockTransport(handler)
        ) as client:
            assert (
                await client.complete([{"role": "user", "content": "Тест"}], model="selected/model")
                == "Ответ"
            )
        assert client._http.is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"message": "private"}},
        {"choices": []},
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "incomplete"}, "finish_reason": "length"}]},
    ],
)
def test_openrouter_rejects_invalid_or_truncated_answers(payload: dict) -> None:
    async def run() -> None:
        async with OpenRouterClient(
            Settings(openrouter_api_key="test-key"),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        ) as client:
            with pytest.raises(APIError):
                await client.complete([{"role": "user", "content": "Тест"}])

    asyncio.run(run())


def test_openrouter_timeout_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private-response", request=request)

    async def run() -> None:
        async with OpenRouterClient(
            Settings(openrouter_api_key="test-key"), transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(APIError, match="openrouter: timeout"):
                await client.complete([{"role": "user", "content": "Тест"}])

    asyncio.run(run())
    assert calls == 1


def test_manager_and_thread_paths_preserve_only_needed_channel_fields() -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v2/managers":
            return httpx.Response(200, json=[{"id": 15, "confirmed": True}])
        assert request.url.path == "/v2/messaging/456/threads"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "source",
                    "realId": 255,
                    "type": "message",
                    "saId": 88,
                    "token": "private-source-token",
                },
                {"id": "other", "realId": None, "type": "message", "saId": 88},
            ],
        )

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="synthetic"), transport=httpx.MockTransport(handler)
        ) as client:
            assert (await client.list_managers())[0]["id"] == 15
            threads = await client.list_threads("456")
            assert threads[0]["realId"] == 255 and threads[1]["realId"] is None
            assert all("token" not in thread for thread in threads)

    asyncio.run(run())
    assert paths == ["/v2/managers", "/v2/messaging/456/threads"]


@pytest.mark.parametrize(
    "source,cursor,messages,error",
    [
        (0, 0, [], None),
        (0, None, [], None),
        (255, None, [], None),
        (255, 1795171785523, [{"messageId": "old"}], None),
        (99, None, [], "invalid_history_response"),
        (True, None, [], "invalid_history_response"),
        (255, None, [{"messageId": "old"}], "invalid_history_response"),
        (255, "1795171785523", [], "invalid_history_response"),
        (255, True, [], "invalid_history_response"),
        (True, 0, [], "invalid_history_response"),
        (255, 0, [], "invalid_history_end"),
        (0, 1, [], "invalid_history_end"),
        (99, 1, [{"messageId": "old"}], "history_source_mismatch"),
        (0, 1, [{"messageId": "old"}], "history_source_mismatch"),
        (255, 0, [{"messageId": "old"}], "invalid_history_cursor"),
    ],
)
def test_history_page_end_and_source_validation(
    source: object,
    cursor: object,
    messages: list,
    error: str | None,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"source": source, "cursor": cursor, "messages": messages})

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="synthetic"), transport=httpx.MockTransport(handler)
        ) as client:
            if error is not None:
                with pytest.raises(APIError, match=error):
                    await client.history_page("456", "255", cursor=1795171785523)
            else:
                page = await client.history_page("456", "255", cursor=1795171785523)
                assert page["source"] == source
                assert page["cursor"] == (0 if cursor is None else cursor)

    asyncio.run(run())
    assert calls == [{"cursor": 1795171785523}]


@pytest.mark.parametrize(
    "payload,diagnostics",
    [
        (["private-body"], ["response_type=list", "messages_type=missing"]),
        ({"messages": [], "cursor": 0}, ["source_type=missing"]),
        (
            {"messages": [], "cursor": "private-cursor", "source": 255},
            ["cursor_type=str", "source_type=int"],
        ),
        ({"messages": [], "source": 255}, ["cursor_type=missing"]),
        (
            {"messages": [{"messageId": "private-id"}], "cursor": None, "source": 255},
            ["cursor_type=NoneType"],
        ),
        ({"messages": [], "cursor": -123, "source": 255}, ["cursor_negative=True"]),
        ({"messages": [], "cursor": 0, "source": True}, ["source_type=bool"]),
        (
            {"messages": {"private-key": "private-text"}, "cursor": 0, "source": 255},
            ["messages_type=dict"],
        ),
        (
            {"messages": ["private-text", {"token": "private-token"}, None], "cursor": 1},
            ["message_item_types=['NoneType', 'dict', 'str']", "source_type=missing"],
        ),
    ],
)
def test_invalid_history_diagnostics_do_not_log_values(payload: object, diagnostics: list) -> None:
    calls = []
    logs = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    async def run() -> None:
        async with UmnicoClient(
            Settings(umnico_api_key="private-api-key"), transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(APIError, match="invalid_history_response"):
                await client.history_page("456", "255")

    sink = logger.add(logs.append, format="{message}")
    try:
        asyncio.run(run())
    finally:
        logger.remove(sink)
    assert len(calls) == 1 and len(logs) == 1
    output = str(logs[0])
    assert all(item in output for item in diagnostics)
    assert all(value not in output for value in ("private-", "456", "255", "-123", "token"))


@pytest.mark.parametrize("cursor", [0, -1, True, "123"])
def test_invalid_history_cursor_never_reaches_network(cursor: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid cursor must be rejected before authentication")

    async def run() -> None:
        async with UmnicoClient(Settings(), transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(APIError, match="invalid_history_cursor"):
                await client.history_page("456", "255", cursor=cursor)

    asyncio.run(run())
