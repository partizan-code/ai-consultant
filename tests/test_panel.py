"""Headless Streamlit actions with synthetic data and a fully substituted backend."""

from dataclasses import replace
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
        settings={"model": "chosen/model", "auto_reply": False},
        paused=False,
        drafts=[{"id": 1, "text": "Ответ", "status": "pending"}],
        attempts=[],
        calls=[],
        comment=False,
        reply_chat_id=None,
        failure=False,
        outcome="sent",
        messages=[],
        chats=[
            {
                "id": 1,
                "name": "Анна",
                "source_id": "sender",
                "source_type": "message",
                "pending_drafts": 1,
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
            return {"calls": [], "last_error": None, "jobs": {}, "worker_enabled": False}
        if path == "knowledge/reload":
            return {"chunks": 2}
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
                "drafts": state.drafts,
                "attempts": state.attempts,
                "reply_block_reason": "comment_private_reply_unverified" if state.comment else None,
                "reply_chat_id": state.reply_chat_id,
            }
        if path == "chats/1/pause":
            state.paused = payload["paused"]
            return payload
        if path == "chats/1/import-history":
            return {"imported": 0}
        if path.endswith("/approve"):
            state.drafts[0]["status"] = state.outcome
            return {"status": state.outcome, "error_code": None}
        if path.endswith("/send-text"):
            state.paused = True
            state.drafts[0]["status"] = "stale"
            state.attempts.append(
                {
                    **payload,
                    "status": state.outcome,
                    "error_code": None,
                    "created_at": "2026-11-20",
                    "confirmed": False,
                }
            )
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
    app.toggle[0].set_value(True)
    button(app, "Сохранить настройки").click().run()
    assert backend.settings == {"model": "new/model", "auto_reply": True}
    button(app, "Обновить базу знаний").click().run()
    assert any("фрагментов — 2" in item.value for item in app.success)
    app.radio(key="panel_section").set_value("Состояние").run()
    assert not app.exception and len(app.warning) == 1


def test_panel_pause_and_approve(backend: SimpleNamespace) -> None:
    app = panel()
    assert app.toggle(key="bot_active_1").value
    app.toggle(key="bot_active_1").set_value(False).run()
    assert backend.paused and button(app, "Одобрить и отправить").disabled
    app.toggle(key="bot_active_1").set_value(True).run()
    assert app.toggle(key="bot_active_1").value and not backend.paused
    assert [payload for method, path, payload in backend.calls if path == "chats/1/pause"] == [
        {"paused": True},
        {"paused": False},
    ]
    button(app, "Одобрить и отправить").click().run()
    assert backend.drafts[0]["status"] == "sent" and not backend.paused
    assert not app.exception and len(app.success) == 1


def test_panel_manual_reply_and_refresh_do_not_resend(backend: SimpleNamespace) -> None:
    app = panel()
    app.text_area[0].set_value("Здравствуйте")
    button(app, "Отправить ответ").click().run()
    assert backend.paused and backend.drafts[0]["status"] == "stale"
    assert not app.toggle(key="bot_active_1").value
    assert len(backend.attempts) == 1
    button(app, "Обновить данные").click().run()
    assert len(backend.attempts) == 1 and not app.exception
    assert app.text_area[0].value == ""


def test_panel_unknown_disables_new_manual_send(backend: SimpleNamespace) -> None:
    backend.outcome = "unknown"
    app = panel()
    app.text_area[0].set_value("Здравствуйте")
    button(app, "Отправить ответ").click().run()
    assert button(app, "Отправить ответ").disabled
    button(app, "Обновить данные").click().run()
    assert len(backend.attempts) == 1 and not app.exception


def test_panel_comment_and_backend_error(backend: SimpleNamespace) -> None:
    backend.comment = True
    app = panel()
    assert button(app, "Одобрить и отправить").disabled
    assert button(app, "Отправить ответ").disabled
    backend.failure = True
    button(app, "Обновить данные").click().run()
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


@pytest.fixture
def timer(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Deliver Streamlit's registered timer event without a browser or wall-clock waits.

    AppTest has no public timer driver. Intercept its runner only in these tests;
    production code uses the public st.fragment API.
    """
    state = SimpleNamespace(active=False, registrations=[])
    original_run = LocalScriptRunner.run
    original_request = ScriptRequests.request_rerun

    def run(runner, *args, **kwargs):
        result = original_run(runner, *args, **kwargs)
        state.registrations.extend(
            (message.auto_rerun.fragment_id, message.auto_rerun.interval)
            for message in runner.forward_msgs()
            if message.HasField("auto_rerun")
        )
        return result

    def request(runner, data):
        if state.active:
            data = replace(data, fragment_id=state.registrations[-1][0], is_auto_rerun=True)
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
    assert timer.registrations and timer.registrations[-1][1] == 3.0
    timer.active = True
    app.run()
    assert not app.exception
    assert any(item.value == "Новое сообщение" for item in app.text)
    assert len(form_calls) == form_count
    assert backend.calls == [("GET", "chats/2", None)]
    assert app.session_state["selected_chat_id"] == 2
    # A subsequent full rerun still has the same text and selected chat.
    timer.active = False
    app.run()
    assert not app.exception and app.text_area[0].value == "Набираемый ответ"
    assert app.button(key="chat_tab_2").proto.type == "primary"
    assert not backend.attempts


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
    button(app, "Обновить данные").click().run()
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
    button(app, "Обновить данные").click().run()
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
    button(app, "Обновить данные").click().run()
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


def test_grouped_chat_uses_direct_route_and_keeps_comment_draft_blocked(
    backend: SimpleNamespace,
) -> None:
    backend.reply_chat_id = 2  # The first event was a comment, Direct arrived later.
    backend.chats[0]["source_types"] = "comment,message"
    backend.drafts[0].update(chat_id=1, source_type="comment")
    app = panel()
    assert not app.exception and button(app, "Одобрить и отправить").disabled
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
