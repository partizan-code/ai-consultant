"""Pinned test conversation UI; all generation goes through the authenticated backend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from ai_consultant_bot.config import Settings
from ai_consultant_bot.panel_client import PanelError, admin_request
from ai_consultant_bot.storage import Lead

TEST_CHAT_ID = "test"


def empty_test_state() -> dict:
    return {"history": [], "lead": asdict(Lead()), "payment_options_mentioned": False}


def reset_test_chat() -> None:
    # Change the editor/form identity too, so a previous submission cannot replay.
    st.session_state["test_chat_revision"] = st.session_state.get("test_chat_revision", 0) + 1
    st.session_state["test_chat_state"] = empty_test_state()
    st.session_state["test_chat_messages"] = []
    st.session_state.pop("test_chat_model", None)


def show_test_chat(
    settings: Settings,
    client_card: DeltaGenerator,
    lead_labels: dict[str, str],
    render_messages: Callable,
) -> None:
    if "test_chat_state" not in st.session_state:
        reset_test_chat()
    state = st.session_state["test_chat_state"]
    revision = st.session_state["test_chat_revision"]
    with client_card.container(border=False, key="client_card"):
        st.subheader("Карточка клиента")
        st.caption("Тестовый клиент")
        for key, label in lead_labels.items():
            with st.container(gap=2):
                st.caption(label)
                st.text(state["lead"][key] or "—")
    with st.container(border=False, key="conversation_window"):
        with st.container(horizontal=True, vertical_alignment="center", key="conversation_header"):
            st.subheader("Тестовый чат")
        render_messages(settings, -1, {"messages": st.session_state["test_chat_messages"]})
        with st.container(key="reply_composer"):
            with st.form(f"test_chat_form_{revision}"):
                text = st.text_area(
                    "Сообщение тестового клиента",
                    label_visibility="collapsed",
                    placeholder="Введите сообщение от лица клиента…",
                    max_chars=settings.history_context_chars,
                    key=f"test_chat_text_{revision}",
                )
                with st.container(
                    horizontal=True,
                    horizontal_alignment="distribute",
                    key="test_chat_actions",
                ):
                    # Keep send first so the form's keyboard shortcut still sends.
                    submitted = st.form_submit_button("Отправить сообщение")
                    st.form_submit_button(
                        "Очистить чат", key="test_chat_reset", on_click=reset_test_chat
                    )
            if submitted:
                if not text.strip():
                    st.info("Введите сообщение.")
                    return
                request_settings = replace(
                    settings,
                    panel_request_timeout=max(
                        settings.panel_request_timeout,
                        settings.openrouter_read_timeout + settings.connect_timeout + 5,
                    ),
                )
                try:
                    with st.spinner("Лейла готовит ответ…"):
                        result = admin_request(
                            request_settings,
                            "POST",
                            "test-chat/reply",
                            {"text": text, "state": state},
                        )
                except PanelError as error:
                    st.error(str(error))
                    return
                if st.session_state["test_chat_revision"] != revision:
                    return
                now = datetime.now(UTC).isoformat()
                additions = [
                    {"direction": direction, "text": value, "occurred_at": now, "imported": False}
                    for direction, value in (("incoming", text), ("outgoing", result["reply"]))
                ]
                st.session_state["test_chat_messages"] = (
                    st.session_state["test_chat_messages"] + additions
                )[-settings.panel_page_size :]
                st.session_state["test_chat_state"] = result["state"]
                st.session_state["test_chat_model"] = result["model"]
                st.session_state["test_chat_revision"] = revision + 1
                st.session_state["chat_scroll_revision"] = (
                    st.session_state.get("chat_scroll_revision", 0) + 1
                )
                st.rerun()
