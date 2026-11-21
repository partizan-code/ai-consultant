"""Configuration checks with synthetic values only."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_consultant_bot.config import Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ai_consultant_bot.config.DEFAULT_ENV_FILE", tmp_path / ".env")


def test_explicit_file_and_environment_precedence(tmp_path: Path) -> None:
    path = tmp_path / "settings.txt"
    path.write_text(
        'DEFAULT_MODEL=file/model\nUMNICO_LOGIN=test\nUMNICO_PASSWORD=" password "\n'
        "DATABASE_PATH=data/test.sqlite3\n",
        encoding="utf-8",
    )
    settings = load_settings(path, environ={"UMNICO_LOGIN": "process-login"})
    assert settings.umnico_login == "process-login"
    assert settings.umnico_password == " password "
    assert settings.database_path == Path("data/test.sqlite3")


def test_missing_default_file_does_not_discover_files_or_mutate_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):
        pytest.fail("Implicit file access")

    monkeypatch.setattr("ai_consultant_bot.config.dotenv_values", fail)
    synthetic_environment = {"ADMIN_TOKEN": "process-token"}
    monkeypatch.setattr(
        "ai_consultant_bot.config.os", SimpleNamespace(environ=synthetic_environment)
    )
    assert load_settings(environ={}).default_model == "openai/gpt-6.1-sol"
    assert load_settings().admin_token == "process-token"
    assert synthetic_environment == {"ADMIN_TOKEN": "process-token"}


def test_default_file_is_loaded_independently_of_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("ADMIN_TOKEN=file-secret\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("ADMIN_TOKEN=wrong-secret\n", encoding="utf-8")
    monkeypatch.chdir(other)
    assert load_settings(environ={}).admin_token == "file-secret"


def test_deployment_settings_are_loaded_and_other_settings_stay_in_python(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("SERVER_PORT=8100\n", encoding="utf-8")
    settings = load_settings(path, environ={"DEFAULT_MODEL": "ignored/model"})
    assert settings == Settings(server_port=8100)
    assert Settings(server_port=9000).server_port == 9000


def test_server_file_and_process_overrides(tmp_path: Path) -> None:
    path = tmp_path / "production.env"
    path.write_text(
        f"APP_ENV=production\nPUBLIC_BASE_URL=https://ai.example.org\n"
        f"DATABASE_PATH={tmp_path.as_posix()}/production.sqlite3\n"
        f"KNOWLEDGE_BASE_PATH={tmp_path.as_posix()}/knowledge\n"
        "SERVER_PORT=8100\nPANEL_PORT=8501\nREGISTER_WEBHOOK=false\nADMIN_TOKEN=file-token\n",
        encoding="utf-8",
    )
    settings = load_settings(
        environ={
            "AI_CONSULTANT_ENV_FILE": str(path),
            "SERVER_PORT": "8105",
            "ADMIN_TOKEN": "process-token",
        }
    )
    assert settings.environment == "production"
    assert settings.public_base_url == "https://ai.example.org"
    assert settings.database_path == tmp_path / "production.sqlite3"
    assert settings.server_port == 8105
    assert settings.admin_token == "process-token"
    assert not settings.register_webhook


def test_server_does_not_fall_back_to_local_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ADMIN_TOKEN=local-secret\n", encoding="utf-8")
    with pytest.raises(ValueError, match="AI_CONSULTANT_ENV_FILE"):
        load_settings(environ={"APP_ENV": "staging"})


@pytest.mark.parametrize(
    "content",
    [
        "SERVER_PORT=invalid",
        "REGISTER_WEBHOOK=yes",
        "DATABASE_PATH=",
        "APP_ENV=prod",
        "PUBLIC_BASE_URL=http://ai.example.org",
        "PUBLIC_BASE_URL=https://ai.example.org/private-token",
        "APP_ENV=production",
        "APP_ENV=staging",
    ],
)
def test_invalid_deployment_file_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.env"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(path, environ={})


def test_explicit_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "absent", environ={})


@pytest.mark.parametrize(
    "values",
    [
        {"umnico_login": "test"},
        {"umnico_password": "test"},
        {"connect_timeout": 0},
        {"read_timeout": float("nan")},
        {"database_timeout": float("inf")},
        {"max_completion_tokens": 0},
        {"default_model": " "},
        {"log_level": "INVALID"},
        {"server_port": 65536},
        {"history_max_pages": 0},
        {"webhook_max_bytes": 0},
        {"server_startup_timeout": 0},
        {"server_shutdown_timeout": 0},
        {"webhook_limit": 0},
        {"webhook_name": " "},
        {"processing_poll_interval": 0},
        {"panel_refresh_interval": 0},
        {"panel_refresh_interval": float("nan")},
        {"panel_refresh_interval": float("inf")},
        {"panel_history_refresh_interval": 0},
        {"panel_history_refresh_interval": float("nan")},
        {"panel_history_refresh_interval": float("inf")},
        {"history_context_messages": 0},
        {"history_context_chars": 0},
        {"knowledge_file_max_bytes": 0},
        {"knowledge_max_chars": 0},
        {"knowledge_max_documents": 0},
        {"post_context_chars": 0},
        {"consultation_reply_max_chars": 0},
        {"lead_field_max_chars": 0},
    ],
)
def test_invalid_configuration(values: dict) -> None:
    with pytest.raises(ValueError):
        Settings(**values)


def test_secrets_are_absent_from_settings_repr() -> None:
    settings = Settings(
        umnico_login="private-login",
        umnico_password="private-password",
        umnico_api_key="private-jwt",
        openrouter_api_key="private-key",
        admin_token="private-admin",
        webhook_token="private-webhook-token",
    )
    assert "private-" not in repr(settings)


def test_step_two_configuration() -> None:
    settings = Settings(
        umnico_send_verified=True,
        umnico_history_verified=True,
        server_port=9000,
        history_max_pages=20,
        webhook_max_bytes=2048,
    )
    assert settings.umnico_send_verified and settings.umnico_history_verified
    assert (settings.server_port, settings.history_max_pages, settings.webhook_max_bytes) == (
        9000,
        20,
        2048,
    )
