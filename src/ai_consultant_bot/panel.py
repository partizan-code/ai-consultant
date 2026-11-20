"""Local single-administrator Streamlit views; all data goes through the backend."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import streamlit as st

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
    "pending": "Ожидает одобрения",
    "approved": "Одобрен",
    "stale": "Устарел",
    "sent": "Принят Umnico",
    "failed": "Ошибка",
    "unknown": "Результат неизвестен — повтор заблокирован",
    "blocked": "Отправка заблокирована",
}
PANEL_STYLES = """
<style>
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
    .st-key-chat_tabs button { text-align: left; justify-content: flex-start; }
    .st-key-chat_tabs [data-testid="stVerticalBlock"] { gap: 0.4rem; }
    .st-key-chat_thread [data-testid="stVerticalBlock"] { gap: 0.25rem; }
    .st-key-chat_thread [data-testid="stChatMessage"] {
        padding: 0.45rem 0.65rem;
        gap: 0.5rem;
        border-radius: 0.65rem;
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
    if pages == 1:
        return 0
    page = st.number_input(label, min_value=1, max_value=pages, step=1, key=key)
    return (int(page) - 1) * settings.panel_page_size


def show_result(result: dict) -> None:
    status = result["status"]
    message = STATUS_LABELS.get(status, status)
    if result.get("error_code"):
        message += f" ({result['error_code']})"
    if status == "sent":
        st.success(message + ". Доставка подтверждается исходящим webhook.")
    else:
        st.warning(message)


def show_chat(settings: Settings, chat_id: int) -> None:
    # The timer never redraws the form: its unsubmitted text stays in the browser.
    st.fragment(run_every=settings.panel_refresh_interval)(show_chat_updates)(settings, chat_id)
    detail = admin_request(settings, "GET", f"chats/{chat_id}")
    show_manual_reply(settings, chat_id, detail)


def show_chat_updates(settings: Settings, chat_id: int) -> None:
    # Catch inside the fragment too: timer reruns do not enter render_panel's try block.
    try:
        show_chat_data(settings, chat_id)
    except PanelError as error:
        st.error(str(error))


def show_chat_data(settings: Settings, chat_id: int) -> None:
    detail = admin_request(settings, "GET", f"chats/{chat_id}")
    paused = bool(detail["chat"]["paused"])
    status, actions = st.columns([3, 1])
    status.caption("Бот на паузе" if paused else "Бот активен")
    with actions:
        if st.button("Возобновить бота" if paused else "Пауза бота", key=f"pause_{chat_id}"):
            admin_request(settings, "POST", f"chats/{chat_id}/pause", {"paused": not paused})
            st.rerun()
    with st.expander("Карточка клиента"):
        st.table(
            [
                {"Поле": label, "Значение": detail["lead"][key] or "—"}
                for key, label in LEAD_LABELS.items()
            ]
        )
    blocked = detail["reply_block_reason"]
    if blocked:
        st.warning("Личный ответ после комментария пока недоступен: " + blocked)
    show_messages(settings, chat_id, detail)
    show_drafts(settings, chat_id, detail)
    show_attempts(detail)


def show_messages(settings: Settings, chat_id: int, detail: dict) -> None:
    st.subheader("Переписка")
    offset = page_offset(
        "Страница переписки (1 — новые)", detail["message_total"], settings, f"messages_{chat_id}"
    )
    if offset:
        detail = admin_request(settings, "GET", f"chats/{chat_id}?offset={offset}")
    with st.container(height=420, border=True, key="chat_thread"):
        if not detail["messages"]:
            st.info("В этом чате пока нет сообщений.")
        for message in detail["messages"]:
            role = "user" if message["direction"] == "incoming" else "assistant"
            with st.chat_message(role):
                st.text(message["text"] or "[Сообщение без текста]")
                if message["post_url"]:
                    st.text("Пост: " + message["post_url"])
                if message["post_description"]:
                    st.text(message["post_description"])
                if message["post_owner"]:
                    st.text("Автор поста: " + message["post_owner"])
                st.caption(
                    format_message_time(message["occurred_at"])
                    + (" · импорт" if message["imported"] else "")
                )
    if st.button("Импортировать историю", key=f"history_{chat_id}"):
        result = admin_request(settings, "POST", f"chats/{chat_id}/import-history")
        st.success(f"Добавлено сообщений: {result['imported']}. Для просмотра обновите данные.")


def show_drafts(settings: Settings, chat_id: int, detail: dict) -> None:
    st.subheader("Черновики")
    for draft in detail["drafts"]:
        with st.expander(
            f"#{draft['id']} · {STATUS_LABELS.get(draft['status'], draft['status'])}",
            expanded=draft["status"] == "pending",
        ):
            st.text(draft["text"])
            if draft["status"] == "pending" and st.button(
                "Одобрить и отправить",
                key=f"approve_{draft['id']}",
                disabled=bool(detail["chat"]["paused"]) or bool(detail["reply_block_reason"]),
            ):
                st.session_state["send_result"] = admin_request(
                    settings, "POST", f"chats/{chat_id}/drafts/{draft['id']}/approve"
                )
                st.rerun()


def show_manual_reply(settings: Settings, chat_id: int, detail: dict) -> None:
    st.subheader("Ручной ответ")
    st.caption("Отправка поставит бота на паузу и отменит прежние черновики.")
    uncertain = any(a["status"] == "unknown" for a in detail["attempts"])
    revision_key = f"manual_revision_{chat_id}"
    revision = st.session_state.get(revision_key, 0)
    pending_key = f"manual_attempt_{chat_id}"
    with st.form(f"manual_{chat_id}_{revision}"):
        text = st.text_area(
            "Текст ответа",
            max_chars=settings.consultation_reply_max_chars,
            key=f"manual_text_{chat_id}_{revision}",
        )
        submit = st.form_submit_button(
            "Отправить ответ", disabled=bool(detail["reply_block_reason"]) or uncertain
        )
    if submit:
        if not text.strip():
            st.warning("Введите текст ответа.")
        else:
            pending = st.session_state.get(pending_key)
            if pending and pending["text"] != text:
                st.warning("Сначала обновите данные и выясните результат предыдущего ответа.")
            else:
                body = pending or {"request_id": str(uuid4()), "text": text}
                st.session_state[pending_key] = body
                result = admin_request(settings, "POST", f"chats/{chat_id}/send-text", body)
                st.session_state["send_result"] = result
                if result["status"] != "unknown":
                    del st.session_state[pending_key]
                    st.session_state[revision_key] = revision + 1
                st.rerun()
    pending = st.session_state.get(pending_key)
    if pending:
        saved = next(
            (a for a in detail["attempts"] if a["request_id"] == pending["request_id"]), None
        )
        if saved:
            show_result(saved)
            if saved["status"] != "unknown":
                del st.session_state[pending_key]
                st.session_state[revision_key] = revision + 1


def show_attempts(detail: dict) -> None:
    if detail["attempts"]:
        with st.expander("Последние попытки отправки"):
            for attempt in detail["attempts"]:
                st.caption(attempt["created_at"] + " · " + STATUS_LABELS[attempt["status"]])
                st.text(attempt["text"])
                if attempt["confirmed"]:
                    st.caption("Подтверждено исходящим сообщением")
                if attempt["error_code"]:
                    st.text(attempt["error_code"])


def show_chats(settings: Settings) -> None:
    listing = admin_request(settings, "GET", "chats")
    if not listing["total"]:
        st.info("Чатов пока нет. Они появятся после первого webhook.")
        return
    chat_list, conversation = st.columns([1, 3], gap="large")
    chat_tabs = chat_list.container(key="chat_tabs")
    with chat_tabs:
        st.subheader("Чаты")
        offset = page_offset("Страница чатов", listing["total"], settings, "chats_page")
        if offset:
            listing = admin_request(settings, "GET", f"chats?offset={offset}")
        chats = {chat["id"]: chat for chat in listing["items"]}
        if not chats:
            st.info("На этой странице нет чатов.")
            return
        if st.session_state.get("selected_chat_id") not in chats:
            st.session_state["selected_chat_id"] = next(iter(chats))
        with st.container(height=560, border=False):
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
                st.caption(f"{chat['source_type']} · черновиков: {chat['pending_drafts']}")
    with conversation:
        chat_id = st.session_state["selected_chat_id"]
        st.subheader(chat_title(chats[chat_id]))
        st.caption(f"Переписка обновляется каждые {settings.panel_refresh_interval:g} с.")
        show_chat(settings, chat_id)


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
    data = admin_request(settings, "GET", "settings")
    with st.form("settings"):
        model = st.text_input(
            "Модель OpenRouter", value=data["model"], max_chars=settings.model_max_chars
        )
        auto_reply = st.toggle("Автоответы в Direct", value=data["auto_reply"])
        st.caption(
            "При выключенных автоответах черновики требуют одобрения. "
            "Модель применяется к следующим заданиям; старые черновики не отправляются автоматически."
        )
        if st.form_submit_button("Сохранить настройки"):
            admin_request(settings, "PUT", "settings", {"model": model, "auto_reply": auto_reply})
            st.success("Настройки сохранены.")
    if st.button("Обновить базу знаний"):
        result = admin_request(settings, "POST", "knowledge/reload")
        st.success(f"База знаний обновлена: фрагментов — {result['chunks']}.")


def render_panel(settings: Settings) -> None:
    st.set_page_config(page_title="Мебельный консультант", page_icon="💬", layout="wide")
    st.html(PANEL_STYLES)
    with st.container(key="panel_header"):
        st.title("Мебельный консультант")
        navigation, refresh = st.columns([4, 1])
        with navigation:
            section = st.radio(
                "Раздел",
                ["Чаты и заявки", "Состояние", "Настройки"],
                horizontal=True,
                label_visibility="collapsed",
                key="panel_section",
            )
        refresh.button("Обновить данные")
        st.divider()
    if "send_result" in st.session_state:
        show_result(st.session_state.pop("send_result"))
    try:
        {"Чаты и заявки": show_chats, "Состояние": show_status, "Настройки": show_settings}[
            section
        ](settings)
    except PanelError as error:
        st.error(str(error))


if __name__ == "__main__":
    try:
        render_panel(load_settings())
    except (OSError, ValueError):
        st.error("Не удалось загрузить конфигурацию. Проверьте настройки и .env.")
