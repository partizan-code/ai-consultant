"""Headless Streamlit actions with synthetic data and a fully substituted backend."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from streamlit.runtime.scriptrunner_utils.script_requests import ScriptRequests
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.local_script_runner import LocalScriptRunner

from ai_consultant_bot.config import Settings
from ai_consultant_bot.panel import format_message_time
from ai_consultant_bot.panel_client import PanelError, admin_request
from ai_consultant_bot.panel_startup import main


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        settings={"model": "chosen/model"},
        paused=True,
        suggestion={"conversation_id": 1, "message_id": 1, "text": "Ответ"},
        attempts=[],
        calls=[],
        comment=False,
        reply_chat_id=None,
        failure=False,
        outcome="sent",
        lose_send_response=False,
        status={"calls": [], "last_error": None, "jobs": {}, "worker_enabled": False},
        messages=[],
        chats=[
            {
                "id": 1,
                "name": "Анна",
                "source_id": "sender",
                "source_type": "message",
            },
        ],
    )

    def request(settings, method, path, payload=None):
        state.calls.append((method, path, payload))
        if state.failure:
            raise PanelError("Синтетический backend недоступен")
        if path == "settings":
            if method == "PUT":
                state.settings = payload
            return state.settings
        if path == "status":
            return state.status
        if path == "knowledge/reload":
            return {"documents": 2}
        if path == "chats" or path.startswith("chats?offset="):
            offset = int(path.partition("offset=")[2] or 0)
            return {
                "total": len(state.chats),
                "items": state.chats[offset : offset + settings.panel_page_size],
            }
        if path in {"chats/1", "chats/2"}:
            return {
                "chat": {
                    "paused": state.paused,
                    "instagram_username": next(
                        chat.get("instagram_username")
                        for chat in state.chats
                        if chat["id"] == int(path.rsplit("/", 1)[1])
                    ),
                },
                "lead": {
                    key: None
                    for key in (
                        "name",
                        "phone",
                        "furniture_type",
                        "dimensions",
                        "budget",
                        "city",
                        "purchase_time",
                    )
                },
                "messages": state.messages,
                "message_total": len(state.messages),
                "suggestion": state.suggestion,
                "attempts": state.attempts,
                "reply_block_reason": "comment_private_reply_unverified" if state.comment else None,
                "reply_chat_id": state.reply_chat_id,
            }
        if path == "chats/1/pause":
            state.paused = payload["paused"]
            if not state.paused:
                state.suggestion = None
            return payload
        if path == "chats/1/import-history":
            return {"imported": 0}
        if path.endswith("/send-text"):
            state.paused = True
            if state.outcome == "sent":
                state.suggestion = None
            state.attempts.append(
                {
                    **payload,
                    "status": state.outcome,
                    "error_code": None,
                    "created_at": "2026-11-20",
                    "confirmed": False,
                }
            )
            if state.lose_send_response:
                raise PanelError("Синтетический ответ backend потерян")
            return state.attempts[-1]
        raise AssertionError(path)

    monkeypatch.setattr("ai_consultant_bot.panel.admin_request", request)
    return state


def panel(*, page_size: int = 50) -> AppTest:
    # Explicit Settings: never read the user's .env or open their database.
    return AppTest.from_string(
        "from ai_consultant_bot.panel import render_panel\n"
        "from ai_consultant_bot.config import Settings\n"
        f"render_panel(Settings(admin_token='synthetic-admin', panel_page_size={page_size}))\n"
    ).run(timeout=30)


def button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def test_panel_settings_status_and_reload(backend: SimpleNamespace) -> None:
    app = panel()
    assert not app.exception
    app.radio(key="panel_section").set_value("Настройки").run()
    app.text_input[0].set_value("new/model")
    button(app, "Сохранить настройки").click().run()
    assert backend.settings == {"model": "new/model"}
    button(app, "Обновить базу знаний").click().run()
    assert any("документов — 2" in item.value for item in app.success)
    app.radio(key="panel_section").set_value("Состояние").run()
    assert not app.exception and len(app.warning) == 1


def test_panel_activation_keeps_editor_and_removes_suggestion(backend: SimpleNamespace) -> None:
    app = panel()
    assert not app.toggle(key="bot_active_1").value
    app.text_area[0].set_value("Мои правки")
    app.toggle(key="bot_active_1").set_value(True).run()
    assert not backend.paused and backend.suggestion is None
    assert app.text_area[0].value == "Мои правки"
    assert not any(item.label == "Использовать подсказку" for item in app.button)
    assert [payload for method, path, payload in backend.calls if path == "chats/1/pause"] == [
        {"paused": False},
    ]
    assert not backend.attempts and not app.exception


def test_panel_manual_reply_and_refresh_do_not_resend(backend: SimpleNamespace) -> None:
    app = panel()
    app.text_area[0].set_value("Здравствуйте")
    button(app, "Отправить ответ").click().run()
    assert backend.paused and backend.suggestion is None
    assert not app.toggle(key="bot_active_1").value
    assert len(backend.attempts) == 1
    app.run()
    assert len(backend.attempts) == 1 and not app.exception
    assert app.text_area[0].value == ""


def test_panel_unknown_disables_new_manual_send(backend: SimpleNamespace) -> None:
    backend.outcome = "unknown"
    app = panel()
    app.text_area[0].set_value("Здравствуйте")
    button(app, "Отправить ответ").click().run()
    assert button(app, "Отправить ответ").disabled
    app.run()
    assert len(backend.attempts) == 1 and not app.exception


def test_send_refreshes_history_immediately_and_restarts_history_timer(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    app = panel()
    history_timers = {key for key, interval in timer.registrations if interval == 3.0}
    assert len(history_timers) == 1
    list_timer = timer.registrations[0]
    assert list_timer[1] == 10.0
    app.text_area[0].set_value("Ответ без ожидания webhook")
    backend.calls.clear()
    timer.active = True
    timer.only = -1
    button(app, "Отправить ответ").click().run()
    assert not app.exception
    assert [message.text[0].value for message in app.chat_message] == ["Ответ без ожидания webhook"]
    assert app.text_area[0].value == ""
    send_index = next(
        i for i, (_, path, _) in enumerate(backend.calls) if path.endswith("send-text")
    )
    assert ("GET", "chats/1", None) in backend.calls[send_index + 1 :]
    assert ("GET", "chats", None) in backend.calls[send_index + 1 :]
    new_timers = {key for key, interval in timer.registrations if interval == 3.0} - history_timers
    assert len(new_timers) == 1
    assert timer.registrations.count(list_timer) >= 2
    # A normal history tick retains the restarted timer and never sends again.
    timer.only = None
    app.run()
    assert not app.exception and len(backend.attempts) == 1
    assert {key for key, interval in timer.registrations if interval == 3.0} == (
        history_timers | new_timers
    )
    # The webhook's confirmed message replaces the immediate preview.
    backend.attempts[0]["confirmed"] = True
    backend.messages.append(
        {
            "direction": "outgoing",
            "text": "Ответ без ожидания webhook",
            "occurred_at": "2026-11-20T16:00:00Z",
            "imported": False,
            "post_url": None,
            "post_description": None,
            "post_owner": None,
        }
    )
    app.run()
    assert not app.exception and len(app.chat_message) == 1
    assert app.chat_message[0].text[0].value == "Ответ без ожидания webhook"


def test_history_has_no_page_selector_and_keeps_latest_messages_after_send(
    backend: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_consultant_bot.panel as views

    backend.messages = [
        {
            "direction": "incoming",
            "text": f"Сообщение {index}",
            "occurred_at": f"2026-11-19T16:0{index}:00Z",
            "imported": False,
            "post_url": None,
            "post_description": None,
            "post_owner": None,
        }
        for index in range(2)
    ]
    original_request = views.admin_request

    def request(settings, method, path, payload=None):
        base, _, offset = path.partition("?offset=")
        result = original_request(settings, method, base, payload)
        if base == "chats/1" and method == "GET":
            index = len(backend.messages) - 1 - int(offset or 0)
            return {**result, "messages": backend.messages[index : index + 1]}
        return result

    monkeypatch.setattr(views, "admin_request", request)
    app = panel(page_size=1)
    app.session_state["messages_1"] = 2
    app.run()
    assert not app.number_input
    assert app.chat_message[0].text[0].value == "Сообщение 1"
    app.text_area[0].set_value("Новый ответ")
    button(app, "Отправить ответ").click().run()
    assert not app.exception and not app.number_input
    assert [message.text[0].value for message in app.chat_message] == [
        "Сообщение 1",
        "Новый ответ",
    ]
    assert len(backend.attempts) == 1


def test_panel_comment_and_backend_error(backend: SimpleNamespace) -> None:
    backend.comment = True
    app = panel()
    assert button(app, "Отправить ответ").disabled
    backend.failure = True
    app.run()
    assert app.error[0].value == "Синтетический backend недоступен" and not app.exception


def test_panel_http_auth_loopback_and_no_retry() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == "Bearer private-admin"
        raise httpx.ReadTimeout("private-detail", request=request)

    with pytest.raises(PanelError) as caught:
        admin_request(
            Settings(admin_token="private-admin"),
            "POST",
            "chats/1/send-text",
            {"text": "private-text"},
            transport=httpx.MockTransport(handler),
        )
    assert len(calls) == 1
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize("limit", [False, True])
def test_panel_knowledge_reload_errors_are_specific_and_safe(limit: bool) -> None:
    def handler(request):
        return httpx.Response(
            409,
            json={
                "detail": {
                    "error_code": "knowledge_limit_exceeded" if limit else "other",
                    "message": "private-body",
                }
            },
        )

    with pytest.raises(PanelError) as caught:
        admin_request(
            Settings(admin_token="synthetic-admin"),
            "POST",
            "knowledge/reload",
            transport=httpx.MockTransport(handler),
        )
    message = str(caught.value)
    assert "Предыдущая загруженная версия сохранена" in message
    assert ("Превышен лимит" if limit else "UTF-8") in message
    assert "private-body" not in message


@pytest.mark.parametrize("status", [302, 400, 401, 404, 409, 503])
def test_panel_http_errors_are_safe(status: int) -> None:
    def handler(request):
        return httpx.Response(
            status, text="private-response", headers={"Location": "https://private.example"}
        )

    with pytest.raises(PanelError) as caught:
        admin_request(
            Settings(admin_token="private-token"),
            "GET",
            "chats",
            transport=httpx.MockTransport(handler),
        )
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize(
    "response,expected",
    [
        (
            httpx.Response(
                409,
                json={"error_code": "invalid_history_response", "detail": "private-body"},
            ),
            "Umnico вернул неожиданный формат ответа",
        ),
        (httpx.Response(409, json={"error_code": "private-code"}), "разделе «Состояние»"),
        (httpx.Response(409, json={"error_code": ["private-code"]}), "разделе «Состояние»"),
        (httpx.Response(409, json=["private-body"]), "разделе «Состояние»"),
        (httpx.Response(409, text="private-invalid-json"), "разделе «Состояние»"),
    ],
)
def test_panel_history_conflict_is_specific_and_safe(
    response: httpx.Response, expected: str
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    with pytest.raises(PanelError) as caught:
        admin_request(
            Settings(admin_token="private-token"),
            "POST",
            "chats/1/import-history",
            transport=httpx.MockTransport(handler),
        )
    message = str(caught.value)
    assert "Не удалось импортировать историю" in message and expected in message
    assert "пауз" not in message and "private-" not in message
    assert len(calls) == 1


def test_panel_launcher_is_local_and_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(admin_token="private-admin")
    monkeypatch.setattr("ai_consultant_bot.panel_startup.load_settings", lambda: settings)
    calls = []
    monkeypatch.setattr(
        "ai_consultant_bot.panel_startup.subprocess.run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert main() == 0
    assert "--server.address=127.0.0.1" in calls[0]
    assert "--server.port=8501" in calls[0]
    assert "private-admin" not in " ".join(calls[0])
    monkeypatch.setattr(
        "ai_consultant_bot.panel_startup.load_settings", lambda: replace(settings, admin_token="")
    )
    assert main() == 1 and len(calls) == 1


def test_server_panel_uses_public_origin_and_keeps_browser_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        environment="production",
        public_base_url="https://ai.example.org",
        database_path=tmp_path / "db.sqlite3",
        knowledge_base_path=tmp_path / "knowledge",
        server_port=8100,
        admin_token="private-admin",
    )
    monkeypatch.setattr("ai_consultant_bot.panel_startup.load_settings", lambda: settings)
    calls = []
    monkeypatch.setattr(
        "ai_consultant_bot.panel_startup.subprocess.run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert main() == 0
    assert "--browser.serverAddress=ai.example.org" in calls[0]
    assert "--browser.serverPort=443" in calls[0]
    assert "--server.enableCORS=true" in calls[0]
    assert "--server.enableXsrfProtection=true" in calls[0]
    assert "--server.fileWatcherType=none" in calls[0]


@pytest.fixture
def timer(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Deliver Streamlit's registered timer event without a browser or wall-clock waits.

    AppTest has no public timer driver. Intercept its runner only in these tests;
    production code uses the public st.fragment API.
    """
    state = SimpleNamespace(active=False, registrations=[], deliver=False, only=None, interval=None)
    original_run = LocalScriptRunner.run
    original_request = ScriptRequests.request_rerun

    def run(runner, *args, **kwargs):
        state.deliver = state.active
        state.fragment_ids = list(
            dict.fromkeys(
                fragment_id
                for fragment_id, _ in state.registrations
                if runner._fragment_storage.contains(fragment_id)
            )
        )
        if state.only is not None:
            state.fragment_ids = [state.fragment_ids[state.only]]
        if state.interval is not None:
            intervals = dict(state.registrations)
            state.fragment_ids = [
                fragment_id
                for fragment_id in state.fragment_ids
                if intervals[fragment_id] == state.interval
            ]
        if state.active:
            # AppTest seeds a full rerun in the constructor; replace it with the
            # timer request rather than coalescing it into a full page redraw.
            runner._requests.on_scriptrunner_ready()
        result = original_run(runner, *args, **kwargs)
        state.registrations.extend(
            (message.auto_rerun.fragment_id, message.auto_rerun.interval)
            for message in runner.forward_msgs()
            if message.HasField("auto_rerun")
        )
        return result

    def request(runner, data):
        if state.deliver:
            state.deliver = False
            data = replace(data, fragment_id_queue=state.fragment_ids, is_auto_rerun=True)
        return original_request(runner, data)

    monkeypatch.setattr(LocalScriptRunner, "run", run)
    monkeypatch.setattr(ScriptRequests, "request_rerun", request)
    return state


def test_timer_updates_chat_without_redrawing_form_or_sending(
    backend: SimpleNamespace, timer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_consultant_bot.panel as views

    original_form = views.show_manual_reply
    form_calls = []

    def form(*args):
        form_calls.append(1)
        return original_form(*args)

    monkeypatch.setattr(views, "show_manual_reply", form)
    backend.chats.append({**backend.chats[0], "id": 2, "name": "Борис"})
    app = panel()
    app.button(key="chat_tab_2").click().run()
    # AppTest omits unsubmitted form values on .run(); seed the stored widget
    # value, then verify a real fragment event never redraws that form.
    app.session_state["manual_text_2_0"] = "Набираемый ответ"
    app.run()
    assert app.text_area[0].value == "Набираемый ответ"
    form_count = len(form_calls)
    backend.messages.append(
        {
            "direction": "incoming",
            "occurred_at": "2026-11-20T16:00:00Z",
            "imported": False,
            "text": "Новое сообщение",
            "post_url": None,
            "post_description": None,
            "post_owner": None,
        }
    )
    backend.calls.clear()
    assert {interval for _, interval in timer.registrations} == {3.0, 10.0}
    timer.active = True
    app.run()
    assert not app.exception
    assert any(item.value == "Новое сообщение" for item in app.text)
    assert len(form_calls) == form_count
    assert backend.calls == [
        ("GET", "chats", None),
        ("GET", "chats/2", None),
        ("GET", "chats/2", None),
        ("GET", "chats/2", None),
        ("GET", "chats/2", None),
    ]
    assert app.session_state["selected_chat_id"] == 2
    # A subsequent full rerun still has the same text and selected chat.
    timer.active = False
    app.run()
    assert not app.exception and app.text_area[0].value == "Набираемый ответ"
    assert app.button(key="chat_tab_2").proto.type == "primary"
    assert not backend.attempts


def test_history_timer_does_not_refresh_other_data_or_editor(
    backend: SimpleNamespace, timer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_consultant_bot.panel as views

    history_runs = []
    original_messages = views.show_messages

    def show_messages(*args):
        history_runs.append(1)
        return original_messages(*args)

    monkeypatch.setattr(views, "show_messages", show_messages)
    app = panel()
    app.session_state["manual_text_1_0"] = "Набираемый ответ"
    backend.messages.append(
        {
            "direction": "incoming",
            "text": "Новое сообщение клиента",
            "occurred_at": "2026-11-21T16:00:00Z",
            "imported": False,
            "post_url": None,
            "post_description": None,
            "post_owner": None,
        }
    )
    backend.paused = False
    backend.calls.clear()
    timer.active = True
    timer.interval = 3.0
    app.run()
    assert not app.exception and backend.calls == [("GET", "chats/1", None)]
    assert app.chat_message[0].text[0].value == "Новое сообщение клиента"
    assert app.session_state["manual_text_1_0"] == "Набираемый ответ"
    assert not app.session_state["bot_active_1"]
    history_count = len(history_runs)
    timer.interval = 10.0
    app.run()
    assert not app.exception and app.session_state["bot_active_1"]
    assert len(history_runs) == history_count
    assert app.session_state["manual_text_1_0"] == "Набираемый ответ"


def test_scroll_initialization_changes_on_full_refresh_but_not_timer(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    def scroll_script(app):
        scripts = [item for item in app.html if item.proto.unsafe_allow_javascript]
        assert len(scripts) == 1
        return scripts[0].value

    app = panel()
    first = scroll_script(app)
    assert 'const view = "1:0:1"' in first
    app.run()
    refreshed = scroll_script(app)
    assert not app.exception and 'const view = "1:0:2"' in refreshed
    timer.active = True
    app.run()
    assert not app.exception and scroll_script(app) == refreshed
    timer.active = False
    app.run()
    assert not app.exception and 'const view = "1:0:3"' in scroll_script(app)
    assert all(method == "GET" for method, _, _ in backend.calls)


def test_chat_tabs_open_only_selected_chat_and_keep_selection(backend: SimpleNamespace) -> None:
    backend.chats.append({**backend.chats[0], "id": 2, "name": "Борис"})
    app = panel()
    assert not app.sidebar.radio and not app.selectbox
    backend.calls.clear()
    app.button(key="chat_tab_2").click().run()
    assert not app.exception
    assert app.session_state["selected_chat_id"] == 2
    assert any(path == "chats/2" for _, path, _ in backend.calls)
    assert not any(path == "chats/1" for _, path, _ in backend.calls)
    assert app.button(key="chat_tab_2").proto.type == "primary"
    assert app.button(key="chat_tab_1").proto.type == "secondary"
    app.radio(key="panel_section").set_value("Настройки").run()
    app.radio(key="panel_section").set_value("Чаты и заявки").run()
    app.run()
    assert not app.exception and app.session_state["selected_chat_id"] == 2
    assert not backend.attempts


def test_chat_tabs_follow_pagination(backend: SimpleNamespace) -> None:
    backend.chats.append({**backend.chats[0], "id": 2, "name": "Борис"})
    app = panel(page_size=1)
    assert app.session_state["selected_chat_id"] == 1
    app.number_input(key="chats_page").set_value(2).run()
    assert not app.exception and app.session_state["selected_chat_id"] == 2
    assert [item.key for item in app.button if (item.key or "").startswith("chat_tab_")] == [
        "chat_tab_2"
    ]
    app.number_input(key="chats_page").set_value(1).run()
    assert not app.exception and app.session_state["selected_chat_id"] == 1


@pytest.mark.parametrize("username, title", [("partizan_code", "partizan_code"), (None, "sender")])
def test_chat_title_uses_login_instead_of_account_or_lead_name(
    backend: SimpleNamespace, username: str | None, title: str
) -> None:
    backend.chats[0]["instagram_username"] = username
    backend.chats[0]["account_name"] = "Старое имя аккаунта"
    app = panel()
    assert not app.exception
    assert app.button(key="chat_tab_1").label == f"{title} · #1"
    assert title not in [item.value for item in app.subheader]
    card = app.main.get_by_key("client_card")
    assert card.caption[0].value == "Instagram"
    if username:
        assert len(card.html) == 1
        assert f'href="https://www.instagram.com/{username}/"' in card.html[0].value
        assert f">{username}</a>" in card.html[0].value
    else:
        assert not card.html and card.text[0].value == "—"
    assert not any("Переписка обновляется" in item.value for item in app.caption)
    assert "Анна" not in [item.value for item in app.subheader]
    assert "Старое имя аккаунта" not in [item.value for item in app.subheader]


def test_login_title_refresh_does_not_request_display_name(backend: SimpleNamespace) -> None:
    backend.chats[0]["instagram_username"] = "partizan_code"
    app = panel()
    # A running Streamlit session may still contain state from the old lookup.
    app.session_state["profile_error_1"] = "Имя аккаунта недоступно"
    app.session_state["refresh_account_profile"] = True
    app.run()
    assert not app.exception and not app.warning
    app.run()
    assert not app.exception and not app.warning
    assert app.button(key="chat_tab_1").label == "partizan_code · #1"
    assert all("/account-profile" not in path for _, path, _ in backend.calls)
    assert all(method == "GET" for method, _, _ in backend.calls)
    assert not backend.attempts


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-11-20T16:04:59Z", "20-11-2026 16:04"),
        ("2026-01-02T03:04:05.123456+03:00", "02-01-2026 03:04"),
        ("2026-11-20 16:04:05", "20-11-2026 16:04"),
        ("", "—"),
        ("invalid", "—"),
    ],
)
def test_message_time_format(timestamp: str, expected: str) -> None:
    assert format_message_time(timestamp) == expected


@pytest.mark.parametrize("photo", [None, "https://example.test/photo.jpg"])
def test_compact_messages_keep_text_context_and_import_marker(
    backend: SimpleNamespace, photo: str | None
) -> None:
    backend.messages = [
        {
            "direction": "incoming",
            "occurred_at": "2026-11-20T16:04:59Z",
            "imported": True,
            "text": "<script>alert('text')</script>\nВторая строка",
            "post_url": "https://example.test/post",
            "post_description": "Описание поста",
            "post_owner": "Автор",
            "post_photo_url": photo,
        },
        {
            "direction": "outgoing",
            "occurred_at": "2026-11-20T16:05:00Z",
            "imported": False,
            "text": None,
            "post_url": None,
            "post_description": None,
            "post_owner": None,
        },
    ]
    app = panel()
    assert not app.exception and len(app.chat_message) == 2
    assert app.chat_message[0].caption[0].value == "20-11-2026 16:04 · импорт"
    assert app.chat_message[1].caption[0].value == "20-11-2026 16:05"
    assert [item.value for item in app.chat_message[0].text] == [
        backend.messages[0]["text"],
        "Пост: https://example.test/post",
        "Описание поста",
        "Автор поста: Автор",
    ]
    assert app.chat_message[1].text[0].value == "[Сообщение без текста]"
    images = app.chat_message[0].get("image")
    assert len(images) == (1 if photo else 0)
    if photo:
        assert images[0].proto.imgs[0].url == photo
    assert not app.chat_message[1].get("image")


def test_grouped_chat_uses_direct_route_for_suggestion(
    backend: SimpleNamespace,
) -> None:
    backend.reply_chat_id = 2  # The first event was a comment, Direct arrived later.
    backend.chats[0]["source_types"] = "comment,message"
    app = panel()
    assert not app.exception and not button(app, "Использовать подсказку").disabled
    assert not button(app, "Отправить ответ").disabled
    app.text_area[0].set_value("Ответ в существующий Direct")
    button(app, "Отправить ответ").click().run()
    assert not app.exception
    sends = [path for method, path, _ in backend.calls if method == "POST"]
    assert sends == ["chats/2/send-text"]
    assert len(backend.attempts) == 1


def test_timer_recovers_from_backend_error(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    app = panel()
    timer.active = True
    backend.failure = True
    app.run()
    assert not app.exception and app.error[0].value == "Синтетический backend недоступен"
    backend.failure = False
    backend.paused = True
    app.run()
    assert not app.exception and not app.error
    assert not app.toggle(key="bot_active_1").value
    assert all(method == "GET" for method, _, _ in backend.calls)


def test_timer_updates_chat_list_without_switching_or_resetting_reply(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    app = panel(page_size=1)
    app.session_state["manual_text_1_0"] = "Набираемый ответ"
    app.run()
    backend.chats.insert(0, {**backend.chats[0], "id": 2, "instagram_username": "new_client"})
    backend.calls.clear()
    timer.active = True
    app.run()
    assert not app.exception
    assert app.button(key="chat_tab_2").label == "new_client · #2"
    assert app.session_state["selected_chat_id"] == 1
    assert app.session_state["manual_text_1_0"] == "Набираемый ответ"
    assert all(method == "GET" for method, _, _ in backend.calls)
    assert not any(item.label == "Обновить данные" for item in app.button)


@pytest.mark.parametrize("initial", ["empty", "unavailable"])
def test_timer_opens_first_chat_after_empty_list_or_initial_error(
    backend: SimpleNamespace, timer: SimpleNamespace, initial: str
) -> None:
    chats = backend.chats
    backend.chats = [] if initial == "empty" else chats
    backend.failure = initial == "unavailable"
    app = panel()
    assert not app.exception and not app.text_area
    assert timer.registrations and all(interval == 10 for _, interval in timer.registrations)
    backend.chats = chats
    backend.failure = False
    backend.calls.clear()
    timer.active = True
    app.run()
    assert not app.exception and not app.error and len(app.text_area) == 1
    assert app.session_state["selected_chat_id"] == 1
    assert all(method == "GET" for method, _, _ in backend.calls)


def test_timer_updates_manual_send_availability_without_redrawing_input(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    backend.comment = True
    app = panel()
    app.session_state["manual_text_1_0"] = "Набираемый ответ"
    app.run()
    assert button(app, "Отправить ответ").disabled
    backend.comment = False
    backend.reply_chat_id = 2
    timer.active = True
    app.run()
    assert not app.exception and not button(app, "Отправить ответ").disabled
    assert app.session_state["manual_text_1_0"] == "Набираемый ответ"
    backend.attempts.append(
        {
            "request_id": "external",
            "status": "unknown",
            "text": "Другой ответ",
            "created_at": "2026-11-21",
            "confirmed": False,
            "error_code": None,
        }
    )
    app.run()
    assert not app.exception and button(app, "Отправить ответ").disabled
    backend.attempts[0]["status"] = "sent"
    backend.attempts[0]["confirmed"] = True
    app.run()
    assert not app.exception and not button(app, "Отправить ответ").disabled
    assert app.session_state["manual_text_1_0"] == "Набираемый ответ"
    # Exercise a real form submission rerun scoped to the controls fragment.
    timer.only = -1
    button(app, "Отправить ответ").click().run()
    assert not app.exception
    assert [path for method, path, _ in backend.calls if method == "POST"] == ["chats/2/send-text"]


def test_timer_recovers_lost_send_response_without_resending(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    backend.lose_send_response = True
    app = panel()
    app.text_area[0].set_value("Здравствуйте")
    timer.active = True
    timer.only = -1
    button(app, "Отправить ответ").click().run()
    assert not app.exception and app.error
    assert len(backend.attempts) == 1
    backend.calls.clear()
    app.run()
    assert app.session_state["manual_revision_1"] == 1
    assert "manual_attempt_1" not in app.session_state
    # Recovery requests a full rerun and changes the controls' fragment ID. Let
    # AppTest rebuild its tree instead of replaying an obsolete timer registration.
    timer.active = False
    app.run()
    assert not app.exception and not app.error and app.text_area[0].value == ""
    assert len(backend.attempts) == 1
    assert all(method == "GET" for method, _, _ in backend.calls)
    # The recovered original text must not become a fresh request on a second click.
    app.session_state["manual_text_1_0"] = "Здравствуйте"
    button(app, "Отправить ответ").click().run()
    assert not app.exception and len(backend.attempts) == 1
    assert app.text_area[0].value == ""
    assert all(method == "GET" for method, _, _ in backend.calls)


def test_timer_keeps_settings_edits_and_shows_current_saved_values(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    app = panel()
    app.radio(key="panel_section").set_value("Настройки").run()
    app.session_state["settings_model"] = "unsaved/model"
    app.run()
    backend.settings = {"model": "external/model"}
    backend.calls.clear()
    timer.active = True
    app.run()
    assert not app.exception
    assert app.session_state["settings_model"] == "unsaved/model"
    assert any("external/model" in item.value for item in app.caption)
    assert backend.calls == [("GET", "settings", None)]


@pytest.mark.parametrize("section", ["Состояние", "Настройки"])
def test_section_timer_recovers_from_initial_error(
    backend: SimpleNamespace, timer: SimpleNamespace, section: str
) -> None:
    app = panel()
    backend.failure = True
    app.radio(key="panel_section").set_value(section).run()
    assert app.error and not app.exception
    backend.failure = False
    backend.status["worker_enabled"] = True
    backend.status["last_error"] = {"code": "synthetic_error"}
    backend.calls.clear()
    timer.active = True
    app.run()
    assert not app.exception and not app.error
    if section == "Состояние":
        assert len(app.table) == 1 and not app.warning
    else:
        assert len(app.text_input) == 1
    assert all(method == "GET" for method, _, _ in backend.calls)


def test_timer_clamps_chat_page_when_list_shrinks(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    backend.chats.append({**backend.chats[0], "id": 2})
    app = panel(page_size=1)
    app.number_input(key="chats_page").set_value(2).run()
    backend.chats.pop()
    timer.active = True
    app.run()
    assert not app.exception and app.session_state["selected_chat_id"] == 1
    backend.chats.clear()
    app.run()
    assert not app.exception and not app.text_area
    assert "selected_chat_id" not in app.session_state


def test_use_suggestion_and_confirm_replacement(backend: SimpleNamespace) -> None:
    app = panel()
    button(app, "Использовать подсказку").click().run()
    assert not app.exception and app.text_area[0].value == "Ответ"
    app.text_area[0].set_value("Мои правки")
    backend.suggestion = {"text": "Новая подсказка", "message_id": 2}
    button(app, "Использовать подсказку").click().run()
    assert not app.exception and app.text_area[0].value == "Мои правки"
    assert any("Заменить" in item.value for item in app.warning)
    button(app, "Отмена").click().run()
    assert app.text_area[0].value == "Мои правки"
    button(app, "Использовать подсказку").click().run()
    button(app, "Подтвердить замену").click().run()
    assert not app.exception and app.text_area[0].value == "Новая подсказка"
    assert not backend.attempts
    app.text_area[0].set_value("Редактированный ответ")
    button(app, "Отправить ответ").click().run()
    assert backend.attempts[0]["text"] == "Редактированный ответ"
    assert app.text_area[0].value == "" and backend.suggestion is None


@pytest.mark.parametrize("outcome", ["failed", "blocked", "unknown"])
def test_unsuccessful_send_preserves_editor(backend: SimpleNamespace, outcome: str) -> None:
    backend.outcome = outcome
    app = panel()
    app.text_area[0].set_value("Сохранить этот текст")
    button(app, "Отправить ответ").click().run()
    app.run()
    assert not app.exception and app.text_area[0].value == "Сохранить этот текст"
    assert backend.suggestion is not None
    assert not app.chat_message
    assert "chat_refresh_revision" not in app.session_state


def test_suggestion_timer_preserves_edits_and_response_inside_window(
    backend: SimpleNamespace, timer: SimpleNamespace
) -> None:
    app = panel()
    window = app.main.get_by_key("conversation_window")
    history = window.get_by_key("chat_thread")
    assert history.proto.height_config.pixel_height == 420
    assert not history.text_area and not history.button
    assert len(window.text_area) == 1
    assert any(item.label == "Отправить ответ" for item in window.button)
    assert any(item.label == "Использовать подсказку" for item in window.button)
    app.session_state["manual_text_1_0"] = "Набираемый ответ"
    app.run()
    backend.suggestion = {"text": "Новая подсказка", "message_id": 2}
    timer.active = True
    app.run()
    assert not app.exception
    assert any(item.value == "Новая подсказка" for item in app.text)
    assert app.session_state["manual_text_1_0"] == "Набираемый ответ"
    assert not backend.attempts
