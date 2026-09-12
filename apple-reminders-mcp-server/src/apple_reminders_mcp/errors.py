from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


REAUTH_HINT = "Run ./auth.sh login on the Raspberry Pi, then restart the service."
