from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import AppError

PRIORITIES = {"none": 0, "high": 1, "medium": 5, "low": 9}
PRIORITY_LABELS = {value: key for key, value in PRIORITIES.items()}


def priority_value(value: str | int | None, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.isdigit():
            value = int(normalized)
        else:
            value = PRIORITIES.get(normalized, -1)
    if value not in PRIORITY_LABELS:
        raise AppError(
            "INVALID_PRIORITY", "Priority must be none, high, medium, low, 0, 1, 5, or 9"
        )
    return int(value)


def parse_datetime(value: str | None, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise AppError("INVALID_TIMEZONE", f"Unknown IANA timezone: {timezone_name}") from exc
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AppError("INVALID_DUE_DATE", "Date must be ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def iso(value: datetime | None, timezone_name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(timezone_name)).isoformat()


def due_payload(value: datetime | None, timezone_name: str) -> dict[str, str] | None:
    if value is None:
        return None
    local = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    local = local.astimezone(ZoneInfo(timezone_name))
    return {
        "local": local.isoformat(),
        "timezone": timezone_name,
        "utc": local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def validate_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise AppError(
            "INVALID_ATTACHMENT_URL",
            "URL attachments require an absolute HTTP(S) URL without embedded credentials",
        )
    return value.strip()


def reminder_payload(
    item: Any, timezone_name: str, *, rich: dict[str, Any] | None = None
) -> dict[str, Any]:
    priority = int(getattr(item, "priority", 0))
    payload: dict[str, Any] = {
        "reminder_id": item.id,
        "list_id": item.list_id,
        "title": item.title,
        "description": getattr(item, "desc", ""),
        "completed": bool(getattr(item, "completed", False)),
        "completed_date": iso(getattr(item, "completed_date", None), timezone_name),
        "due": due_payload(getattr(item, "due_date", None), timezone_name),
        "priority": {"value": priority, "label": PRIORITY_LABELS.get(priority, "unknown")},
        "flagged": bool(getattr(item, "flagged", False)),
        "all_day": bool(getattr(item, "all_day", False)),
        "timezone": getattr(item, "time_zone", None) or timezone_name,
        "parent_reminder_id": getattr(item, "parent_reminder_id", None),
        "created": iso(getattr(item, "created", None), timezone_name),
        "modified": iso(getattr(item, "modified", None), timezone_name),
        "revision": getattr(item, "record_change_tag", None),
    }
    payload.update(rich or {})
    return payload
