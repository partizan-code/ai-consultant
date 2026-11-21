"""Application settings and automatic loading of secrets from the project .env."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
SECRET_FIELDS = (
    "umnico_login",
    "umnico_password",
    "umnico_api_key",
    "openrouter_api_key",
    "admin_token",
    "webhook_token",
)


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/consultant.sqlite3")
    knowledge_base_path: Path = Path("knowledge_base")
    log_level: str = "INFO"
    default_model: str = "openai/gpt-6.1-sol"
    umnico_base_url: str = "https://api.umnico.com"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    umnico_login: str = field(default="", repr=False)
    umnico_password: str = field(default="", repr=False)
    umnico_api_key: str = field(default="", repr=False)
    # Explicit sender used in the confirmed test account; change for another account.
    umnico_user_id: str = "2583899"
    openrouter_api_key: str = field(default="", repr=False)
    admin_token: str = field(default="", repr=False)
    webhook_token: str = field(default="", repr=False)
    umnico_history_verified: bool = True
    umnico_send_verified: bool = True
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    server_startup_timeout: float = 10.0
    server_shutdown_timeout: float = 5.0
    webhook_name: str = "AI Consultant Bot"
    webhook_limit: int = 10
    history_max_pages: int = 100
    webhook_max_bytes: int = 1_048_576
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    openrouter_read_timeout: float = 60.0
    database_timeout: float = 5.0
    token_refresh_margin: float = 30.0
    max_completion_tokens: int = 1000
    processing_poll_interval: float = 1.0
    history_context_messages: int = 30
    history_context_chars: int = 16000
    knowledge_file_max_bytes: int = 1_048_576
    knowledge_max_chars: int = 50_000
    knowledge_max_documents: int = 100
    post_context_chars: int = 1500
    consultation_reply_max_chars: int = 450
    lead_field_max_chars: int = 300
    panel_host: str = "127.0.0.1"
    panel_port: int = 8501
    panel_page_size: int = 50
    panel_request_timeout: float = 30.0
    panel_refresh_interval: float = 10.0
    panel_history_refresh_interval: float = 3.0
    model_max_chars: int = 200

    def __post_init__(self) -> None:
        if bool(self.umnico_login) != bool(self.umnico_password):
            raise ValueError("UMNICO_LOGIN and UMNICO_PASSWORD must be set together")
        for name in (
            "connect_timeout",
            "read_timeout",
            "openrouter_read_timeout",
            "database_timeout",
            "server_startup_timeout",
            "server_shutdown_timeout",
            "processing_poll_interval",
            "panel_request_timeout",
            "panel_refresh_interval",
            "panel_history_refresh_interval",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.token_refresh_margin) or self.token_refresh_margin < 0:
            raise ValueError("token_refresh_margin must be finite and nonnegative")
        if self.max_completion_tokens <= 0:
            raise ValueError("max_completion_tokens must be positive")
        for name in (
            "history_context_messages",
            "history_context_chars",
            "knowledge_file_max_bytes",
            "knowledge_max_chars",
            "knowledge_max_documents",
            "post_context_chars",
            "consultation_reply_max_chars",
            "lead_field_max_chars",
            "panel_page_size",
            "model_max_chars",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 1 <= self.server_port <= 65535:
            raise ValueError("server_port must be between 1 and 65535")
        if not 1 <= self.panel_port <= 65535 or self.panel_port == self.server_port:
            raise ValueError("panel_port must be valid and different from server_port")
        if self.panel_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Panel must listen on localhost")
        if self.history_max_pages <= 0 or self.webhook_max_bytes <= 0:
            raise ValueError("History and webhook limits must be positive")
        if self.webhook_limit <= 0 or not self.webhook_name.strip():
            raise ValueError("Webhook limit and name must be set")
        if not self.default_model.strip():
            raise ValueError("DEFAULT_MODEL must not be empty")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")


def load_settings(
    env_file: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Settings:
    """Load only secrets; all other values are configured through Settings in Python.

    The default file is beside pyproject.toml in this source project. A caller may
    supply another file programmatically. Environment secrets take precedence.
    No parent search, interpolation, environment mutation or network access occurs.
    """
    values: dict[str, str] = {}
    path = DEFAULT_ENV_FILE if env_file is None else env_file
    if env_file is not None or path.exists():
        with path.open(encoding="utf-8") as stream:
            values.update(
                (key, value)
                for key, value in dotenv_values(stream=stream, interpolate=False).items()
                if value is not None
            )
    values.update(os.environ if environ is None else environ)
    kwargs: dict[str, str] = {}
    for name in SECRET_FIELDS:
        key = name.upper()
        if key not in values:
            continue
        value = values[key]
        kwargs[name] = value if name == "umnico_password" else value.strip()
    return Settings(**kwargs)
