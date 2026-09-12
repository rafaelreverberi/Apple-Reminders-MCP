from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .config import Settings
from .errors import REAUTH_HINT, AppError

_DEFAULT_ICLOUD_TIMEOUT = (10.0, 60.0)


def ensure_ca_bundle() -> str:
    """Give raw TLS sockets the maintained CA bundle used by requests.

    Python.org macOS installations can have no populated OpenSSL default CA
    file. pyicloud's trusted-device 2FA bridge uses ``ssl.create_default_context``
    directly, so it needs SSL_CERT_FILE even though normal requests already use
    certifi. Existing operator overrides always win.
    """
    import certifi

    ca_file = os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_file)
    return ca_file


def _install_default_request_timeout(api: Any) -> None:
    """Bound pyicloud setup/PCS calls that otherwise pass ``timeout=None``.

    CloudKit Reminders requests already use this connect/read timeout. pyicloud's
    service bootstrap requests do not, so a stalled PCS refresh could otherwise
    block the synchronous MCP worker indefinitely.
    """
    session = api.session
    if getattr(session, "_apple_reminders_mcp_timeout_installed", False):
        return
    original: Callable[..., Any] = session.request

    def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _DEFAULT_ICLOUD_TIMEOUT
        return original(*args, **kwargs)

    session.request = request_with_timeout
    session._apple_reminders_mcp_timeout_installed = True


def create_saved_session(settings: Settings) -> Any:
    """Hydrate only pyicloud's saved session; never prompt or authenticate."""
    if not settings.username:
        raise AppError(
            "REAUTHENTICATION_REQUIRED",
            "ICLOUD_USERNAME is not configured",
            details={"operator_hint": REAUTH_HINT},
        )
    ensure_ca_bundle()
    from pyicloud import PyiCloudService

    settings.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    api = PyiCloudService(
        settings.username,
        password=None,
        cookie_directory=str(settings.session_dir),
        china_mainland=settings.china_mainland,
        authenticate=False,
        with_family=False,
    )
    _install_default_request_timeout(api)
    status = api.get_auth_status()
    if not status.get("authenticated") or status.get("requires_2fa") or status.get("requires_2sa"):
        raise AppError(
            "REAUTHENTICATION_REQUIRED",
            "The saved iCloud session is not usable",
            details={"operator_hint": REAUTH_HINT},
        )
    if not status.get("trusted_session"):
        raise AppError(
            "REAUTHENTICATION_REQUIRED",
            "The saved iCloud session is not trusted",
            details={"operator_hint": REAUTH_HINT},
        )
    return api


def sanitized_status(settings: Settings, factory=create_saved_session) -> dict[str, Any]:
    base = {
        "authenticated": False,
        "trusted_session": False,
        "requires_2fa": False,
        "requires_2sa": False,
        "requires_reauthentication": True,
        "china_mainland": settings.china_mainland,
        "reminders_available": False,
    }
    try:
        api = factory(settings)
        status = api.get_auth_status()
        base.update(
            {
                "authenticated": bool(status.get("authenticated")),
                "trusted_session": bool(status.get("trusted_session")),
                "requires_2fa": bool(status.get("requires_2fa")),
                "requires_2sa": bool(status.get("requires_2sa")),
                "requires_reauthentication": not bool(
                    status.get("authenticated") and status.get("trusted_session")
                ),
                "reminders_available": getattr(api, "reminders", None) is not None,
            }
        )
    except AppError:
        base["operator_hint"] = REAUTH_HINT
    except Exception:
        base["error_code"] = "ICLOUD_UNAVAILABLE"
    return base
