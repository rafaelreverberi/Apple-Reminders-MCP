from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import AppError

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise AppError("INVALID_CONFIGURATION", f"{name} must be true or false")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise AppError("INVALID_CONFIGURATION", f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AppError("INVALID_CONFIGURATION", f"{name} must be between {minimum} and {maximum}")
    return value


def _csv(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    username: str
    china_mainland: bool
    timezone: str
    default_list: str | None
    allowed_lists: tuple[str, ...]
    writable_lists: tuple[str, ...]
    writes_enabled: bool
    deletes_enabled: bool
    host: str
    port: int
    path: str
    session_dir: Path
    state_dir: Path
    confirmation_secret: str
    confirmation_ttl: int
    read_rate: int
    write_rate: int
    max_reminders: int

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(__file__).resolve().parents[3]
        host = os.getenv("MCP_HOST", "127.0.0.1").strip()
        if host not in LOOPBACK_HOSTS:
            raise AppError("INVALID_CONFIGURATION", "MCP_HOST must be a loopback host")
        path = os.getenv("MCP_PATH", "/mcp").strip()
        if not path.startswith("/") or "?" in path or "#" in path:
            raise AppError("INVALID_CONFIGURATION", "MCP_PATH must be an absolute URL path")
        timezone = os.getenv("DEFAULT_TIMEZONE", "Europe/Zurich").strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise AppError("INVALID_TIMEZONE", f"Unknown IANA timezone: {timezone}") from exc
        session_raw = os.getenv("ICLOUD_SESSION_DIR", "state/pyicloud-session")
        session_dir = Path(session_raw).expanduser()
        if not session_dir.is_absolute():
            session_dir = root / session_dir
        state_dir = root / "state"
        return cls(
            username=os.getenv("ICLOUD_USERNAME", "").strip(),
            china_mainland=_bool("ICLOUD_CHINA_MAINLAND", False),
            timezone=timezone,
            default_list=os.getenv("DEFAULT_REMINDER_LIST", "").strip() or None,
            allowed_lists=_csv("ALLOWED_REMINDER_LISTS"),
            writable_lists=_csv("WRITABLE_REMINDER_LISTS"),
            writes_enabled=_bool("WRITE_ACTIONS_ENABLED", True),
            deletes_enabled=_bool("DELETE_ACTIONS_ENABLED", True),
            host=host,
            port=_int("MCP_PORT", 13003, 1, 65535),
            path=path,
            session_dir=session_dir,
            state_dir=state_dir,
            confirmation_secret=os.getenv("CONFIRMATION_SIGNING_SECRET", ""),
            confirmation_ttl=_int("CONFIRMATION_TTL_SECONDS", 300, 30, 3600),
            read_rate=_int("READ_RATE_LIMIT_PER_MINUTE", 120, 1, 10000),
            write_rate=_int("WRITE_RATE_LIMIT_PER_MINUTE", 30, 1, 1000),
            max_reminders=_int("MAX_REMINDERS_PER_REQUEST", 200, 1, 1000),
        )
