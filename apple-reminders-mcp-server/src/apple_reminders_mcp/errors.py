from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException,
    PyiCloudPasswordException,
    PyiCloudPCSTimeoutException,
)
from pyicloud.services.reminders.client import RemindersAuthError


class AppError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


REAUTH_HINT = "Run ./auth.sh login on the Raspberry Pi, then restart the service."
ICLOUD_DATA_ACCESS_APPROVAL_HINT = (
    "Check your trusted iPhone, iPad, or Mac and approve temporary access to iCloud data, "
    "then retry the operation."
)
ICLOUD_DATA_ACCESS_APPROVAL_MESSAGE = (
    "Apple requires approval from a trusted device before Reminders can be accessed."
)


def exception_chain(exc: BaseException) -> list[BaseException]:
    """Return the complete explicit/implicit cause chain without looping."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _evidence_values(exc: BaseException) -> list[Any]:
    """Collect error metadata for classification only; values are never returned or logged."""
    values: list[Any] = []
    for item in exception_chain(exc):
        values.extend((type(item).__name__, str(item)))
        for attribute in ("reason", "code", "payload"):
            value = getattr(item, attribute, None)
            if value is not None:
                values.append(value)
        response = getattr(item, "response", None)
        if response is None:
            continue
        values.extend(
            value
            for value in (
                getattr(response, "status_code", None),
                getattr(response, "reason", None),
                getattr(response, "text", None),
            )
            if value is not None
        )
        try:
            values.append(response.json())
        except (AttributeError, TypeError, ValueError):
            pass
    return values


def _flatten_evidence(values: list[Any]) -> tuple[str, list[Mapping[Any, Any]]]:
    text: list[str] = []
    mappings: list[Mapping[Any, Any]] = []
    pending = list(values)
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, Mapping):
            mappings.append(value)
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
        elif value is not None:
            text.append(str(value).casefold())
    return " ".join(text), mappings


def is_icloud_data_access_approval_error(exc: BaseException) -> bool:
    """Detect Apple's trusted-device approval flow from specific PCS evidence."""
    chain = exception_chain(exc)
    if any(isinstance(item, PyiCloudPCSTimeoutException) for item in chain):
        return True

    text, mappings = _flatten_evidence(_evidence_values(exc))
    for payload in mappings:
        consent = next(
            (
                value
                for key, value in payload.items()
                if str(key).casefold() == "isdeviceconsentedforpcs"
            ),
            None,
        )
        if consent is False:
            return True

    explicit_markers = (
        "requested the device to upload cookies",
        "cookies not available yet on server",
        "temporary access to icloud data",
        "temporarily access your icloud data",
        "approve temporary access",
        "device consent for pcs",
    )
    return any(marker in text for marker in explicit_markers)


def classify_icloud_error(exc: Exception, *, include_not_found: bool = False) -> AppError:
    """Map pyicloud/CloudKit failures to sanitized, machine-readable errors."""
    chain = exception_chain(exc)
    text, mappings = _flatten_evidence(_evidence_values(exc))
    names = " ".join(type(item).__name__.casefold() for item in chain)

    if is_icloud_data_access_approval_error(exc):
        return AppError(
            "ICLOUD_DATA_ACCESS_APPROVAL_REQUIRED",
            ICLOUD_DATA_ACCESS_APPROVAL_MESSAGE,
            details={
                "operator_hint": ICLOUD_DATA_ACCESS_APPROVAL_HINT,
                "retryable": True,
            },
        )
    if "rate" in names or "http 429" in text or any(
        any(str(key).casefold() == "retry_after" for key in payload) for payload in mappings
    ):
        return AppError("RATE_LIMITED", "Apple is rate limiting Reminders requests")
    if "terms" in names or "terms" in text:
        return AppError(
            "ICLOUD_TERMS_REQUIRED", "Updated iCloud terms require operator review"
        )
    authentication_errors = (
        RemindersAuthError,
        PyiCloud2FARequiredException,
        PyiCloud2SARequiredException,
        PyiCloudAuthRequiredException,
        PyiCloudFailedLoginException,
        PyiCloudPasswordException,
    )
    authentication_status = any(
        getattr(item, "code", None) in (401, 403, "401", "403")
        or getattr(getattr(item, "response", None), "status_code", None) in (401, 403)
        for item in chain
    )
    if (
        any(isinstance(item, authentication_errors) for item in chain)
        or authentication_status
        or any(marker in text for marker in ("http 401", "http 403"))
    ):
        return AppError(
            "REAUTHENTICATION_REQUIRED",
            "iCloud authentication must be renewed",
            details={"operator_hint": REAUTH_HINT},
        )
    if include_not_found and (
        any(isinstance(item, (KeyError, LookupError)) for item in chain) or "not found" in text
    ):
        return AppError("REMINDER_NOT_FOUND", "Reminder was not found")
    return AppError("ICLOUD_UNAVAILABLE", "iCloud Reminders is temporarily unavailable")
