"""Bounded, authenticated loopback HTTP access for the Streamlit panel."""

from __future__ import annotations

from typing import Any

import httpx

from ai_consultant_bot.config import Settings


class PanelError(Exception):
    """Safe display text, without HTTP headers, tokens or response bodies."""


def admin_request(
    settings: Settings,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not settings.admin_token:
        raise PanelError("Задайте ADMIN_TOKEN и перезапустите backend и панель.")
    # Never send the admin token to the tunnel or a configurable remote hostname.
    url = f"http://127.0.0.1:{settings.server_port}/admin/{path}"
    try:
        with httpx.Client(
            timeout=httpx.Timeout(settings.panel_request_timeout, connect=settings.connect_timeout),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {settings.admin_token}"},
                json=payload,
            )
    except httpx.HTTPError:
        raise PanelError(
            "Backend недоступен или время ожидания истекло. Дождитесь автообновления: "
            "результат отправки мог сохраниться на сервере."
        ) from None
    if not response.is_success:
        messages = {
            400: "Проверьте введённые значения.",
            401: "ADMIN_TOKEN панели и backend не совпадает.",
            404: "Запись не найдена. Дождитесь автоматического обновления данных.",
            409: "Действие заблокировано. После автообновления проверьте паузу и состояние отправки.",
            503: "Backend не готов: проверьте ADMIN_TOKEN и доступность SQLite.",
        }
        raise PanelError(messages.get(response.status_code, "Запрос backend не выполнен."))
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise PanelError(
            "Backend вернул некорректный ответ. Дождитесь автоматического обновления данных."
        ) from None
