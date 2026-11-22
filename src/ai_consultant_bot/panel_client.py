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
        if path == "test-chat/reply":
            raise PanelError(
                "Не удалось дождаться тестового ответа. Диалог не изменён; "
                "автоматического повтора запроса нет."
            ) from None
        raise PanelError(
            "Backend недоступен или время ожидания истекло. Дождитесь автообновления: "
            "результат отправки мог сохраниться на сервере."
        ) from None
    if not response.is_success:
        messages = {
            400: "Проверьте введённые значения.",
            401: "ADMIN_TOKEN панели и backend не совпадает.",
            404: "Запись не найдена. Дождитесь автоматического обновления данных.",
            409: "Действие не выполнено. Проверьте состояние операции в разделе «Состояние».",
            503: "Backend не готов: проверьте ADMIN_TOKEN и доступность SQLite.",
        }
        if response.status_code == 409:
            if path == "test-chat/reply":
                messages[409] = (
                    "Модель не вернула тестовый ответ. Проверьте ключ и доступ к OpenRouter."
                )
            if path == "knowledge/reload":
                messages[409] = (
                    "Не удалось обновить базу знаний. Проверьте доступность файлов и кодировку "
                    "UTF-8. Предыдущая загруженная версия сохранена."
                )
            if path.endswith("/import-history"):
                messages[409] = (
                    "Не удалось импортировать историю. Проверьте код ошибки в разделе «Состояние»."
                )
            try:
                error = response.json()
            except ValueError:
                error = None
            if path == "knowledge/reload" and isinstance(error, dict):
                detail = error.get("detail")
                if (
                    isinstance(detail, dict)
                    and detail.get("error_code") == "knowledge_limit_exceeded"
                ):
                    messages[409] = (
                        "Превышен лимит объёма или числа файлов базы знаний. "
                        "Уменьшите базу и повторите обновление. "
                        "Предыдущая загруженная версия сохранена."
                    )
            if isinstance(error, dict) and error.get("error_code") == "invalid_history_response":
                messages[409] = (
                    "Не удалось импортировать историю: Umnico вернул неожиданный формат ответа. "
                    "Диагностика типов полей записана в лог backend."
                )
            if path == "test-chat/reply" and isinstance(error, dict):
                messages[409] = {
                    "external_api_disabled": "В этом окружении тестовые запросы к модели отключены.",
                    "invalid_consultation_response": (
                        "Модель вернула ответ, не соответствующий формату консультации. "
                        "Диалог не изменён."
                    ),
                    "missing_or_invalid_api_key": "Задайте корректный ключ OpenRouter для backend.",
                }.get(error.get("error_code"), messages[409])
        if response.status_code == 503 and path == "test-chat/reply":
            messages[503] = (
                "Тест модели недоступен: проверьте настройки backend и отдельный файл ключа."
            )
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
