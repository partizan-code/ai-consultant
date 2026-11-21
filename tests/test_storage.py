"""Persistence contracts on temporary databases, without services or credentials."""

from dataclasses import replace
from pathlib import Path

import pytest

from ai_consultant_bot.config import Settings
from ai_consultant_bot.storage import (
    ChatKey,
    Lead,
    get_conversation_lead,
    get_setting,
    initialize_database,
    open_database,
    payment_options_mentioned,
    save_chat,
    save_lead,
    save_message,
    set_setting,
)


def test_settings_survive_reinitialization_and_reopening(tmp_path: Path) -> None:
    path = tmp_path / "data" / "test.sqlite3"
    with open_database(path, timeout=1) as connection:
        initialize_database(connection, Settings())
        with connection:
            set_setting(connection, "model", "chosen/model")
    with open_database(path, timeout=1) as connection:
        initialize_database(connection, Settings(default_model="other/model"))
        assert get_setting(connection, "model") == "chosen/model"
        with pytest.raises(ValueError):
            set_setting(connection, "admin_token", "must-not-be-stored")


def test_deduplication_and_different_sources(tmp_path: Path) -> None:
    with open_database(tmp_path / "test.sqlite3", timeout=1) as connection:
        initialize_database(connection, Settings())
        key = ChatKey("account", "lead", "integration", "message", "source")
        with connection:
            direct = save_chat(connection, key)
            assert save_chat(connection, key) == direct
            comment = save_chat(connection, replace(key, source_type="comment"))
            assert direct != comment
            args = dict(
                external_id="external-1",
                direction="incoming",
                text="hello",
                occurred_at="2026-11-20T10:00:00Z",
                imported=True,
            )
            assert save_message(connection, chat_id=direct, **args)
            assert not save_message(connection, chat_id=direct, **args)
            assert save_message(connection, chat_id=comment, **args)
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert connection.execute("SELECT imported FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_partial_lead_updates_preserve_known_fields(tmp_path: Path) -> None:
    with open_database(tmp_path / "test.sqlite3", timeout=1) as connection:
        initialize_database(connection, Settings())
        with connection:
            chat_id = save_chat(connection, ChatKey("a", "l", "i", "message", "s"))
            save_lead(connection, chat_id, Lead(name="Тест", furniture_type="диван"))
            save_lead(connection, chat_id, Lead(city="Минск"))
        lead = connection.execute("SELECT * FROM leads WHERE chat_id = ?", (chat_id,)).fetchone()
        assert lead["name"] == "Тест"
        assert lead["furniture_type"] == "диван"
        assert lead["city"] == "Минск"
        assert lead["phone"] is None


def test_transaction_rollback(tmp_path: Path) -> None:
    with open_database(tmp_path / "test.sqlite3", timeout=1) as connection:
        initialize_database(connection, Settings())
        with pytest.raises(RuntimeError), connection:
            save_chat(connection, ChatKey("a", "l", "i", "message", "s"))
            raise RuntimeError("Simulated processing failure")
        assert connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0


def test_retracted_field_cannot_resurface_from_another_route(tmp_path: Path) -> None:
    with open_database(tmp_path / "test.sqlite3", timeout=1) as connection:
        initialize_database(connection, Settings())
        with connection:
            key = ChatKey("a", "l", "i", "message", "s")
            direct = save_chat(connection, key)
            comment = save_chat(connection, replace(key, source_type="comment"))
            other = save_chat(connection, replace(key, lead_id="other"))
            save_lead(connection, direct, Lead(phone="old", name="Анна"))
            save_lead(connection, comment, Lead(phone="new"))
            save_lead(connection, other, Lead(phone="unrelated"))
            save_lead(connection, comment, Lead(), cleared_fields=("phone",))
        assert get_conversation_lead(connection, direct).phone is None
        assert get_conversation_lead(connection, comment).phone is None
        assert get_conversation_lead(connection, direct).name == "Анна"
        assert get_conversation_lead(connection, other).phone == "unrelated"
        with connection:
            save_lead(connection, direct, Lead(city="Минск"))
        assert get_conversation_lead(connection, direct).phone is None


@pytest.mark.parametrize(
    "field,lead",
    [
        ("phone", Lead(phone="new")),
        ("not_a_field", Lead()),
        ("phone = NULL; DELETE FROM leads", Lead()),
    ],
)
def test_invalid_retractions_do_not_change_existing_data(tmp_path: Path, field, lead) -> None:
    with open_database(tmp_path / "test.sqlite3", timeout=1) as connection:
        initialize_database(connection, Settings())
        with connection:
            chat_id = save_chat(connection, ChatKey("a", "l", "i", "message", "s"))
            save_lead(connection, chat_id, Lead(phone="old"))
        with pytest.raises(ValueError), connection:
            save_lead(connection, chat_id, lead, cleared_fields=(field,))
        assert get_conversation_lead(connection, chat_id).phone == "old"


@pytest.mark.parametrize(
    "status,expected",
    [
        ("sent", True),
        ("unknown", True),
        ("failed", False),
        ("blocked", False),
    ],
)
def test_payment_memory_counts_only_sent_or_uncertain_attempts(
    tmp_path: Path, status, expected
) -> None:
    path = tmp_path / "test.sqlite3"
    with open_database(path, timeout=1) as connection:
        initialize_database(connection, Settings())
        with connection:
            key = ChatKey("a", "l", "i", "message", "s")
            chat_id = save_chat(connection, key)
            comment = save_chat(connection, replace(key, source_type="comment"))
            unrelated = save_chat(connection, replace(key, integration_id="other"))
            connection.execute(
                "INSERT INTO outgoing_attempts(request_id, chat_id, text, status) VALUES (?, ?, ?, ?)",
                ("offer", chat_id, "Есть РАССРОЧКА.", status),
            )
    with open_database(path, timeout=1) as connection:
        assert payment_options_mentioned(connection, chat_id) is expected
        assert payment_options_mentioned(connection, comment) is expected
        assert not payment_options_mentioned(connection, unrelated)
