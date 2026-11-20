"""Parse observed Umnico message envelopes, retaining only useful, non-secret fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ai_consultant_bot.storage import ChatKey


@dataclass(frozen=True)
class MessageEvent:
    chat: ChatKey
    external_id: str
    direction: str
    text: str
    occurred_at: str
    source_real_id: str | None = None
    sender_id: str | None = None
    post_url: str | None = None
    post_description: str | None = None
    post_owner: str | None = None
    custom_id: str | None = None


def identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Invalid identifier")
    result = str(value).strip()
    if not result or len(result) > 512:
        raise ValueError("Invalid identifier")
    return result


def optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def parse_message(account_id: Any, lead_id: Any, message: Any) -> MessageEvent:
    """Parse a webhook message without retaining raw payloads or source tokens."""
    try:
        if not isinstance(message, dict):
            raise ValueError
        source, integration, content = message["source"], message["sa"], message["message"]
        if not all(isinstance(part, dict) for part in (source, integration, content)):
            raise ValueError
        source_type = source["type"]
        if source_type not in {"message", "comment"}:
            raise ValueError
        integration_id = identifier(integration["id"])
        if source.get("saId") is not None and identifier(source["saId"]) != integration_id:
            raise ValueError
        incoming = message["incoming"]
        if not isinstance(incoming, bool):
            raise ValueError
        occurred_at = message["datetime"]
        if not isinstance(occurred_at, str):
            raise ValueError
        if datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
        text = content.get("text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError
        preview = message.get("preview")
        preview = preview if isinstance(preview, dict) else {}
        sender = message.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        real_id = source.get("realId")
        sender_id = sender.get("socialId", source.get("sender"))
        return MessageEvent(
            chat=ChatKey(
                identifier(account_id),
                identifier(lead_id),
                integration_id,
                source_type,
                identifier(source["id"]),
            ),
            external_id=identifier(message["messageId"]),
            direction="incoming" if incoming else "outgoing",
            text=text,
            occurred_at=occurred_at,
            source_real_id=identifier(real_id) if real_id is not None else None,
            sender_id=identifier(sender_id) if sender_id is not None else None,
            post_url=optional_text(content.get("url")) or optional_text(preview.get("url")),
            post_description=optional_text(preview.get("description")),
            post_owner=optional_text(preview.get("owner")),
            custom_id=(
                identifier(message["customId"])
                if not incoming and message.get("customId") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Invalid Umnico message") from None


def history_timestamp(value: Any) -> str:
    """Accept zoned ISO 8601 or Unix milliseconds; never guess a local timezone."""
    try:
        if isinstance(value, str) and not value.isdecimal():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        else:
            if isinstance(value, str) and value.isascii() and value.isdecimal():
                value = int(value)
            if type(value) is not int or value < 0:
                raise ValueError
            parsed = datetime.fromtimestamp(value / 1000, UTC)
        return parsed.astimezone(UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        raise ValueError("Invalid history timestamp") from None


def parse_history_message(route: dict[str, Any], message: dict[str, Any]) -> MessageEvent:
    """Use the validated page route for optional channel fields, checking supplied ones.

    A message without messageId is rejected rather than inventing an identity that
    could later trigger a reply when the same old message arrives by webhook.
    """
    source = {
        "id": route["source_id"],
        "realId": route["source_real_id"],
        "type": route["source_type"],
        "saId": route["integration_id"],
    }
    supplied = message.get("source")
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise ValueError("Invalid history source")
        for key, expected in source.items():
            if supplied.get(key) is not None and identifier(supplied[key]) != str(expected):
                raise ValueError("History source mismatch")
        # Null optional fields do not erase the validated page route.
        source.update({key: value for key, value in supplied.items() if value is not None})
    normalized = {**message, "source": source, "datetime": history_timestamp(message.get("sentAt"))}
    if message.get("sa") is None:
        normalized["sa"] = {"id": route["integration_id"]}
    event = parse_message(route["account_id"], route["lead_id"], normalized)
    if (
        event.chat
        != ChatKey(
            route["account_id"],
            route["lead_id"],
            route["integration_id"],
            route["source_type"],
            route["source_id"],
        )
        or event.source_real_id != route["source_real_id"]
    ):
        raise ValueError("History route mismatch")
    return event


def parse_event(payload: Any) -> MessageEvent | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ValueError("Invalid Umnico event")
    # Lead notifications repeat message text; they must never create reply jobs.
    if payload["type"] not in {"message.incoming", "message.outgoing"}:
        return None
    try:
        message = payload["message"]
        if payload["type"] == "message.outgoing" and payload.get("customId") is not None:
            if not isinstance(message, dict):
                raise ValueError("Invalid Umnico message")
            if message.get("customId") is not None and identifier(
                message["customId"]
            ) != identifier(payload["customId"]):
                raise ValueError("Conflicting custom ID")
            message = {**message, "customId": payload["customId"]}
        event = parse_message(payload["accountId"], payload["leadId"], message)
    except KeyError:
        raise ValueError("Invalid Umnico event") from None
    if payload["type"] != f"message.{event.direction}":
        raise ValueError("Inconsistent message direction")
    return event
