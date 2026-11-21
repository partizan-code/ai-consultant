"""Local single-administrator Streamlit views; all data goes through the backend."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from ai_consultant_bot.config import Settings, load_settings
from ai_consultant_bot.panel_client import PanelError, admin_request

LEAD_LABELS = {
    "name": "Имя",
    "phone": "Телефон",
    "furniture_type": "Мебель",
    "dimensions": "Размеры",
    "budget": "Бюджет",
    "city": "Город",
    "purchase_time": "Срок покупки",
}
STATUS_LABELS = {
    "sent": "Принят Umnico",
    "failed": "Ошибка",
    "unknown": "Результат неизвестен — повтор заблокирован",
    "blocked": "Отправка заблокирована",
}
CHAT_SCROLL_SCRIPT = Path(__file__).with_name("chat_scroll.js").read_text(encoding="utf-8")
PANEL_STYLES = """
<style>
    .st-key-chat_scroll_init { display: none; }
    /* Keep content below Streamlit's fixed toolbar. */
    .block-container { padding-top: 4rem; }
    .block-container :is(h1, h2, h3, h4, h5, h6) {
        font-size: 1.125rem;
        line-height: 1.35;
    }
    .st-key-panel_header [data-testid="stVerticalBlock"] { gap: 0.35rem; }
    .st-key-panel_header h1 {
        padding: 0;
        margin: 0;
    }
    .st-key-panel_header hr { margin: 0.35rem 0; }
    /* Horizontal layouts remove heading padding; normalize all three column titles. */
    :is(.st-key-chat_tabs, .st-key-conversation_header, .st-key-client_card)
        [data-testid="stHeading"] {
        min-height: 2.5rem;
        display: flex;
        align-items: center;
    }
    :is(.st-key-chat_tabs, .st-key-conversation_header, .st-key-client_card)
        [data-testid="stHeading"] [data-testid="stMarkdownContainer"] {
        margin-bottom: 0;
    }
    :is(.st-key-chat_tabs, .st-key-conversation_header, .st-key-client_card)
        [data-testid="stHeading"] h3 {
        padding: 0;
        margin: 0;
    }
    .st-key-chat_tabs button { text-align: left; justify-content: flex-start; }
    .st-key-chat_tabs [data-testid="stVerticalBlock"] { gap: 0.4rem; }
    /* Streamlit inserts layout wrappers between keyed containers and columns. */
    .st-key-chat_layout [data-testid="stColumn"] {
        box-sizing: border-box;
        padding-inline: 1rem;
    }
    .st-key-chat_layout [data-testid="stColumn"]:has(.st-key-chat_tabs) {
        padding-left: 0;
    }
    .st-key-chat_layout [data-testid="stColumn"]:has(.st-key-client_card) {
        padding-right: 0;
    }
    @media (min-width: 641px) {
        /* Reserve the viewport once, including the toolbar clearance and bottom padding. */
        .block-container:has(.st-key-chat_layout) {
            box-sizing: border-box;
            height: 100dvh;
            min-height: 0;
            padding-bottom: 0.75rem;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        /* Carry the available height through Streamlit's layout and fragment wrappers. */
        .block-container :is(
            [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlock"]
        ):has(.st-key-chat_layout),
        .st-key-chat_layout,
        .st-key-chat_layout :is(
            [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlock"],
            [data-testid="stHorizontalBlock"]
        ):has(.st-key-conversation_window),
        .st-key-conversation_window,
        .st-key-conversation_window :is(
            [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlock"]
        ):has(.st-key-chat_thread) {
            flex: 1 1 0;
            min-height: 0;
            height: 100%;
        }
        .st-key-chat_layout [data-testid="stColumn"] {
            display: flex;
            flex-direction: column;
            height: 100%;
            min-height: 0;
            overflow: hidden;
        }
        .st-key-chat_layout [data-testid="stColumn"] {
            min-width: 0;
        }
        .st-key-chat_layout [data-testid="stColumn"]:has(.st-key-conversation_window) {
            border-inline: 1px solid var(--st-border-color, rgba(128, 128, 128, 0.25));
        }
        .st-key-chat_thread {
            height: 100%;
            min-height: 0;
            flex: 1 1 0;
            overflow-y: auto;
            overflow-x: hidden;
        }
        .st-key-conversation_window [data-testid="stLayoutWrapper"]:has(
            > .st-key-reply_composer
        ),
        .st-key-reply_composer {
            flex: 0 0 auto;
            height: auto;
        }
        .st-key-reply_composer textarea {
            resize: none;
        }
        .st-key-suggestion_preview {
            max-height: min(18dvh, 8rem);
            overflow-y: auto;
        }
        .st-key-chat_layout [data-testid="stColumn"] > [data-testid="stVerticalBlock"],
        .st-key-chat_layout :is(
            [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlock"]
        ):has(.st-key-client_card),
        .st-key-chat_layout :is(
            [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlock"]
        ):has(.st-key-chat_list_scroll) {
            flex: 1 1 0;
            height: 100%;
            min-height: 0;
        }
        .st-key-chat_list_scroll {
            flex: 1 1 0;
            height: 100%;
            min-height: 0;
            overflow-y: auto;
        }
        .st-key-client_card {
            height: 100%;
            min-height: 0;
            overflow-y: auto;
        }
    }
    .st-key-chat_thread {
        border-top: 1px solid var(--st-border-color, rgba(128, 128, 128, 0.25));
        border-bottom: 1px solid var(--st-border-color, rgba(128, 128, 128, 0.25));
        border-radius: 0;
        box-sizing: border-box;
        width: 100%;
        max-width: 100%;
    }
    .st-key-suggestion_preview [data-testid="stText"] {
        font-size: 0.875rem;
        line-height: 1.45;
    }
    .st-key-suggestion_preview [data-testid="stText"] > span {
        color: #52bf7c;
    }
    .st-key-chat_thread [data-testid="stVerticalBlock"] { gap: 0.25rem; }
    .st-key-chat_thread [data-testid="stChatMessage"] {
        padding: 0.45rem 0.65rem;
        gap: 0.5rem;
        border-radius: 0.65rem;
    }
    .st-key-chat_thread [data-testid="stChatMessage"]:has(
        [data-testid="stChatMessageAvatarAssistant"]
    ) {
        width: fit-content;
        max-width: min(70%, 38rem);
        margin-left: auto;
        flex-direction: row-reverse;
    }
    .st-key-chat_thread [data-testid="stChatMessageContent"] {
        margin: 0;
        flex-grow: 0;
    }
    .st-key-chat_thread [data-testid="stChatMessage"]:has(
        [data-testid="stChatMessageAvatarAssistant"]
    ) [data-testid="stCaptionContainer"] {
        text-align: right;
    }
    .st-key-chat_thread [data-testid="stText"] {
        font-size: 0.9rem;
        line-height: 1.35;
    }
    .st-key-chat_thread [data-testid="stCaptionContainer"] {
        /* Streamlit's negative markdown margin assumes a full paragraph margin. */
        margin: 0;
        font-size: 0.68rem;
        line-height: 1.25;
        opacity: 0.65;
    }
    .st-key-chat_thread [data-testid="stCaptionContainer"] p {
        margin: 0;
        font-size: inherit;
        line-height: inherit;
    }
</style>
"""


def format_message_time(value: str) -> str:
    """Format the stored timestamp without changing its time zone."""
    try:
        return datetime.fromisoformat(value).strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return "—"


def page_offset(label: str, total: int, settings: Settings, key: str) -> int:
    pages = max(1, (total + settings.panel_page_size - 1) // settings.panel_page_size)
    if key in st.session_state:
        st.session_state[key] = min(max(1, st.session_state[key]), pages)
    if pages == 1:
        return 0
    page = st.number_input(label, min_value=1, max_value=pages, step=1, key=key)
    return (int(page) - 1) * settings.panel_page_size


def show_result(result: dict) -> None:
    status = result["status"]
    if status == "sent":
        return
    message = STATUS_LABELS.get(status, status)
    if result.get("error_code"):
        message += f" ({result['error_code']})"
    st.warning(message)


def run_history_refresh(function: Callable[..., None], settings: Settings, *args: object) -> None:
    def refresh() -> None:
        function(settings, *args)

    # Streamlit retains a timer for the same function and call site across app reruns.
    # A new identity after sending starts a fresh interval for the history.
    revision = st.session_state.get("chat_refresh_revision", 0)
    refresh.__qualname__ = f"{function.__qualname__}_refresh_{revision}"
    st.fragment(run_every=settings.panel_history_refresh_interval)(refresh)()


def refresh_after_send() -> None:
    st.session_state["chat_refresh_revision"] = st.session_state.get("chat_refresh_revision", 0) + 1
    st.rerun()


def show_chat(settings: Settings, chat_id: int, client_card: DeltaGenerator) -> None:
    # A full rerun opens at the latest message; timer updates follow only at the bottom.
    st.session_state["chat_scroll_revision"] = st.session_state.get("chat_scroll_revision", 0) + 1
    # The timer redraws data and buttons only; the editor keeps its widget state.
    with st.container(border=False, key="conversation_window"):
        st.fragment(run_every=settings.panel_refresh_interval)(show_chat_updates)(
            settings, chat_id, client_card
        )
        run_history_refresh(show_history_updates, settings, chat_id)
        show_manual_reply(settings, chat_id)


def show_chat_updates(settings: Settings, chat_id: int, client_card: DeltaGenerator) -> None:
    # Catch inside the fragment too: timer reruns do not enter render_panel's try block.
    try:
        show_chat_data(settings, chat_id, client_card)
    except PanelError as error:
        st.error(str(error))


def show_history_updates(settings: Settings, chat_id: int) -> None:
    try:
        show_messages(settings, chat_id, admin_request(settings, "GET", f"chats/{chat_id}"))
    except PanelError as error:
        st.error(str(error))


def change_bot_state(settings: Settings, chat_id: int) -> None:
    try:
        admin_request(
            settings,
            "POST",
            f"chats/{chat_id}/pause",
            {"paused": not st.session_state[f"bot_active_{chat_id}"]},
        )
    except PanelError as error:
        st.session_state[f"bot_state_error_{chat_id}"] = str(error)


def show_chat_data(settings: Settings, chat_id: int, client_card: DeltaGenerator) -> None:
    detail = admin_request(settings, "GET", f"chats/{chat_id}")
    st.session_state[f"chat_attempts_{chat_id}"] = detail["attempts"]
    # Replace the whole card on timer reruns instead of appending to the right column.
    with client_card.container(border=False, key="client_card"):
        st.subheader("Карточка клиента")
        with st.container(gap=2):
            st.caption("Instagram")
            username = detail["chat"].get("instagram_username")
            if username:
                profile_url = f"https://www.instagram.com/{quote(username, safe='')}/"
                st.html(
                    f'<a href="{profile_url}" target="_blank" rel="noopener noreferrer">'
                    f"{escape(username)}</a>"
                )
            else:
                st.text("—")
        for key, label in LEAD_LABELS.items():
            with st.container(gap=2):
                st.caption(label)
                st.text(detail["lead"][key] or "—")
    paused = bool(detail["chat"]["paused"])
    # Backend state wins after manual replies, timer updates, and failed changes.
    st.session_state[f"bot_active_{chat_id}"] = not paused
    with st.container(horizontal=True, vertical_alignment="center", key="conversation_header"):
        st.subheader("Переписка")
        st.toggle(
            "Бот отключен" if paused else "Бот активен",
            key=f"bot_active_{chat_id}",
            on_change=change_bot_state,
            args=(settings, chat_id),
            width=200,
        )
    if error := st.session_state.pop(f"bot_state_error_{chat_id}", None):
        st.error(error)
    blocked = detail["reply_block_reason"]
    if blocked:
        if blocked == "ambiguous_direct_route":
            st.warning("Найдено несколько каналов Direct; маршрут ответа требует уточнения.")
        else:
            st.warning("Личный ответ после комментария пока недоступен: " + blocked)


def show_messages(settings: Settings, chat_id: int, detail: dict) -> None:
    messages = detail["messages"]
    pending = [
        {
            "direction": "outgoing",
            "text": attempt["text"],
            "occurred_at": attempt["created_at"],
            "imported": False,
        }
        for attempt in detail.get("attempts", [])
        if attempt["status"] == "sent" and not attempt["confirmed"]
    ]
    if pending:

        def message_time(message: dict) -> datetime:
            timestamp = datetime.fromisoformat(message["occurred_at"])
            return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)

        messages = sorted([*messages, *pending], key=message_time)
    # The script follows updates at the bottom without interrupting history reading.
    with st.container(height=420, border=False, key="chat_thread", autoscroll=False):
        if not messages:
            st.info("В этом чате пока нет сообщений.")
        for message in messages:
            role = "user" if message["direction"] == "incoming" else "assistant"
            with st.chat_message(role):
                st.text(message["text"] or "[Сообщение без текста]")
                if message.get("post_url"):
                    st.text("Пост: " + message["post_url"])
                if message.get("post_description"):
                    st.text(message["post_description"])
                if message.get("post_owner"):
                    st.text("Автор поста: " + message["post_owner"])
                if message.get("post_photo_url"):
                    st.image(message["post_photo_url"], width=260)
                st.caption(
                    format_message_time(message["occurred_at"])
                    + (" · импорт" if message["imported"] else "")
                )
    with st.container(key="chat_scroll_init"):
        # Only integer view IDs enter this script; message content stays in st.text.
        revision = st.session_state.get("chat_scroll_revision", 0)
        script = CHAT_SCROLL_SCRIPT.replace("__CHAT_VIEW__", f"{chat_id}:0:{revision}")
        st.html(f"<script>{script}</script>", unsafe_allow_javascript=True)


def use_suggestion(chat_id: int, revision: int, text: str) -> None:
    st.session_state[f"manual_revision_{chat_id}"] = revision + 1
    st.session_state[f"manual_text_{chat_id}_{revision + 1}"] = text
    st.session_state.pop(f"suggestion_replace_{chat_id}", None)
    st.rerun()


def show_suggestion(settings: Settings, chat_id: int) -> None:
    try:
        detail = admin_request(settings, "GET", f"chats/{chat_id}")
    except PanelError as error:
        st.error(str(error))
        return
    suggestion = detail["suggestion"] if detail["chat"]["paused"] else None
    if suggestion:
        with st.container(key="suggestion_preview", gap="xxsmall"):
            st.caption("Подсказка ИИ")
            st.text(suggestion["text"])


def confirm_suggestion(
    chat_id: int, revision: int, suggestion: dict | None, selected: bool
) -> None:
    confirm_key = f"suggestion_replace_{chat_id}"
    if not suggestion:
        st.session_state.pop(confirm_key, None)
        return
    if selected:
        if st.session_state.get(f"manual_text_{chat_id}_{revision}", ""):
            st.session_state[confirm_key] = suggestion["text"]
        else:
            use_suggestion(chat_id, revision, suggestion["text"])
    if confirm_key in st.session_state:
        st.warning("Заменить набранный текст подсказкой?")
        if st.session_state[confirm_key] != suggestion["text"]:
            st.text(st.session_state[confirm_key])
        if st.button("Подтвердить замену"):
            use_suggestion(chat_id, revision, st.session_state[confirm_key])
        if st.button("Отмена"):
            del st.session_state[confirm_key]
            st.rerun()


def show_manual_reply(settings: Settings, chat_id: int) -> None:
    revision_key = f"manual_revision_{chat_id}"
    pending_key = f"manual_attempt_{chat_id}"
    pending = st.session_state.get(pending_key)
    attempts = st.session_state.get(f"chat_attempts_{chat_id}", [])
    # A full rerun can clear a recovered send before creating the input widget.
    if pending and any(
        a["request_id"] == pending["request_id"] and a["status"] == "sent" for a in attempts
    ):
        del st.session_state[pending_key]
        revision = st.session_state.get(revision_key, 0)
        if st.session_state.get(f"manual_text_{chat_id}_{revision}", "") == pending["text"]:
            st.session_state[revision_key] = revision + 1
    revision = st.session_state.get(revision_key, 0)
    with st.container(key="reply_composer"):
        st.fragment(run_every=settings.panel_refresh_interval)(show_suggestion)(settings, chat_id)
        st.text_area(
            "Текст ответа",
            label_visibility="collapsed",
            max_chars=settings.consultation_reply_max_chars,
            key=f"manual_text_{chat_id}_{revision}",
            placeholder=(
                "Введите сообщение... Отправка поставит бота на паузу.\n"
                "Подсказки появляются после нового сообщения клиента"
            ),
        )
        # Refresh controls independently so typing is never overwritten by the timer.
        st.fragment(run_every=settings.panel_refresh_interval)(show_manual_updates)(
            settings, chat_id, revision
        )


def show_manual_updates(settings: Settings, chat_id: int, revision: int) -> None:
    try:
        detail = admin_request(settings, "GET", f"chats/{chat_id}")
    except PanelError as error:
        st.error(str(error))
        st.button("Отправить ответ", disabled=True)
        return
    uncertain = any(a["status"] == "unknown" for a in detail["attempts"])
    suggestion = detail["suggestion"] if detail["chat"]["paused"] else None
    with st.container(horizontal=True, vertical_alignment="center", key=f"reply_actions_{chat_id}"):
        history = st.button("Импортировать историю", key=f"history_{chat_id}")
        selected = st.button("Использовать подсказку") if suggestion else False
        submit = st.button(
            "Отправить ответ", disabled=bool(detail["reply_block_reason"]) or uncertain
        )
    if history:
        route_id = detail.get("reply_chat_id") or chat_id
        try:
            result = admin_request(settings, "POST", f"chats/{route_id}/import-history")
            st.success(
                f"Добавлено сообщений: {result['imported']}. Переписка обновится автоматически."
            )
        except PanelError as error:
            st.error(str(error))
    confirm_suggestion(chat_id, revision, suggestion, selected)
    revision_key = f"manual_revision_{chat_id}"
    pending_key = f"manual_attempt_{chat_id}"
    pending = st.session_state.get(pending_key)
    saved = next(
        (a for a in detail["attempts"] if pending and a["request_id"] == pending["request_id"]),
        None,
    )
    if saved:
        show_result(saved)
        if saved["status"] == "sent" and not submit:
            del st.session_state[pending_key]
            if st.session_state.get(f"manual_text_{chat_id}_{revision}", "") == pending["text"]:
                st.session_state[revision_key] = revision + 1
            st.session_state["send_result"] = saved
            refresh_after_send()
    if submit:
        text = st.session_state.get(f"manual_text_{chat_id}_{revision}", "")
        # Never turn a recovered successful send into a second outgoing request.
        if saved and saved["status"] != "unknown":
            del st.session_state[pending_key]
            if saved["status"] == "sent" and pending["text"] == text:
                st.session_state[revision_key] = revision + 1
                refresh_after_send()
            pending = None
        if not text.strip():
            st.warning("Введите текст ответа.")
        else:
            if pending and pending["text"] != text:
                st.warning("Дождитесь обновления и выясните результат предыдущего ответа.")
            else:
                body = pending or {"request_id": str(uuid4()), "text": text}
                st.session_state[pending_key] = body
                route_id = detail.get("reply_chat_id") or chat_id
                try:
                    result = admin_request(settings, "POST", f"chats/{route_id}/send-text", body)
                except PanelError as error:
                    st.error(str(error))
                    return
                st.session_state["send_result"] = result
                if result["status"] != "unknown":
                    del st.session_state[pending_key]
                    if result["status"] == "sent":
                        st.session_state[revision_key] = revision + 1
                        refresh_after_send()
                st.rerun()


def show_chats(settings: Settings) -> None:
    # The old left column used 1/4 of the width; 3/16 is 25% narrower.
    with st.container(key="chat_layout"):
        chat_list, conversation, client = st.columns([3, 10, 3], gap=None)
    client_card = client.empty()
    chat_tabs = chat_list.container(key="chat_tabs")
    with chat_tabs:
        st.fragment(run_every=settings.panel_refresh_interval)(show_chat_list)(settings)
    if chat_id := st.session_state.get("selected_chat_id"):
        with conversation:
            show_chat(settings, chat_id, client_card)


def show_chat_list(settings: Settings) -> None:
    # Register the timer before the first request, even with no chats or no backend.
    st.subheader("Чаты")
    try:
        listing = admin_request(settings, "GET", "chats")
        if not listing["total"]:
            if st.session_state.pop("selected_chat_id", None) is not None:
                st.rerun()
            st.info("Чатов пока нет. Они появятся после первого webhook.")
            return
        offset = page_offset("Страница чатов", listing["total"], settings, "chats_page")
        if offset:
            listing = admin_request(settings, "GET", f"chats?offset={offset}")
        chats = {chat["id"]: chat for chat in listing["items"]}
        if not chats:
            st.info("На этой странице нет чатов.")
            return
        page_changed = st.session_state.get("chat_list_offset", offset) != offset
        st.session_state["chat_list_offset"] = offset
        if st.session_state.get("selected_chat_id") is None or (
            page_changed and st.session_state["selected_chat_id"] not in chats
        ):
            st.session_state["selected_chat_id"] = next(iter(chats))
            st.rerun()
        with st.container(height=560, border=False, key="chat_list_scroll"):
            for chat_id, chat in chats.items():
                name = chat_title(chat)
                if st.button(
                    f"{name} · #{chat_id}",
                    key=f"chat_tab_{chat_id}",
                    type="primary"
                    if st.session_state["selected_chat_id"] == chat_id
                    else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["selected_chat_id"] = chat_id
                    st.rerun()
    except PanelError as error:
        st.error(str(error))


def chat_title(chat: dict) -> str:
    return chat.get("instagram_username") or chat["source_id"]


def show_status(settings: Settings) -> None:
    data = admin_request(settings, "GET", "status")
    if not data["worker_enabled"]:
        st.warning("Ключ OpenRouter не задан; обработчик консультаций выключен.")
    st.subheader("Последняя ошибка")
    if data["last_error"]:
        st.table([data["last_error"]])
    else:
        st.info("Ошибок API пока нет.")
    st.subheader("Последние обращения")
    if data["calls"]:
        st.dataframe(data["calls"], hide_index=True)
    else:
        st.info("Обращений к API пока нет.")


def show_settings(settings: Settings) -> None:
    st.fragment(run_every=settings.panel_refresh_interval)(show_settings_updates)(settings)
    data = st.session_state.get("panel_settings")
    if data is None:
        return
    with st.form("settings"):
        model = st.text_input(
            "Модель OpenRouter",
            value=data["model"],
            max_chars=settings.model_max_chars,
            key="settings_model",
        )
        st.caption("Модель применяется к следующим заданиям. Активность задаётся в переписке.")
        if st.form_submit_button("Сохранить настройки"):
            admin_request(settings, "PUT", "settings", {"model": model})
            st.success("Настройки сохранены.")
    if st.button("Обновить базу знаний"):
        result = admin_request(settings, "POST", "knowledge/reload")
        st.success(f"База знаний обновлена: документов — {result['documents']}.")


def show_settings_updates(settings: Settings) -> None:
    try:
        data = admin_request(settings, "GET", "settings")
    except PanelError as error:
        st.error(str(error))
        return
    first_load = "panel_settings" not in st.session_state
    st.session_state["panel_settings"] = data
    if first_load:
        st.rerun()
    # Keep saved values current without overwriting unsaved editor input.
    st.caption(f"Сохранено: модель {data['model']}.")


def show_status_updates(settings: Settings) -> None:
    try:
        show_status(settings)
    except PanelError as error:
        st.error(str(error))


def render_panel(settings: Settings) -> None:
    st.set_page_config(page_title="Мебельный консультант", page_icon="💬", layout="wide")
    st.html(PANEL_STYLES)
    with st.container(key="panel_header"):
        st.title("Мебельный консультант")
        section = st.radio(
            "Раздел",
            ["Чаты и заявки", "Состояние", "Настройки"],
            horizontal=True,
            label_visibility="collapsed",
            key="panel_section",
        )
        st.divider()
    if "send_result" in st.session_state:
        show_result(st.session_state.pop("send_result"))
    try:
        if section == "Состояние":
            st.fragment(run_every=settings.panel_refresh_interval)(show_status_updates)(settings)
        else:
            {"Чаты и заявки": show_chats, "Настройки": show_settings}[section](settings)
    except PanelError as error:
        st.error(str(error))


if __name__ == "__main__":
    try:
        render_panel(load_settings())
    except (OSError, ValueError):
        st.error("Не удалось загрузить конфигурацию. Проверьте настройки и .env.")
