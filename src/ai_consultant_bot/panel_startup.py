"""Launch Streamlit on localhost with configuration from Settings."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from loguru import logger

from ai_consultant_bot.config import load_settings
from ai_consultant_bot.logging_config import configure_logging


def main() -> int:
    try:
        settings = load_settings()
    except (OSError, ValueError):
        logger.error("Не удалось загрузить настройки панели.")
        return 1
    configure_logging(settings.log_level)
    if not settings.admin_token:
        logger.error("Для панели необходим ADMIN_TOKEN в .env.")
        return 1
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(Path(__file__).with_name("panel.py")),
        f"--server.address={settings.panel_host}",
        f"--server.port={settings.panel_port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    try:
        return subprocess.run(command, check=False).returncode
    except KeyboardInterrupt:
        return 0
    except OSError:
        logger.error("Не удалось запустить Streamlit.")
        return 1
