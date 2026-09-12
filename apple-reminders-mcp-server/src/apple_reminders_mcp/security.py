from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .errors import AppError


class RateLimiter:
    def __init__(self, reads: int, writes: int):
        self.limits = {"read": reads, "write": writes}
        self.events = {"read": deque(), "write": deque()}
        self.lock = threading.Lock()

    def check(self, kind: str) -> None:
        now = time.monotonic()
        with self.lock:
            bucket = self.events[kind]
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= self.limits[kind]:
                raise AppError("RATE_LIMITED", f"{kind.title()} rate limit exceeded; retry later")
            bucket.append(now)


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS idempotency (
                  request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                  reminder_id TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS confirmations (
                  nonce TEXT PRIMARY KEY, used_at INTEGER);
            """)
        os.chmod(path, 0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def get_idempotency(self, request_id: str, request_hash: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT request_hash, reminder_id, status FROM idempotency WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if not row:
            return None
        if not hmac.compare_digest(row[0], request_hash):
            raise AppError(
                "IDEMPOTENCY_CONFLICT", "request_id was already used with different input"
            )
        if row[2] == "pending":
            raise AppError(
                "UNKNOWN_REMOTE_STATE",
                "A previous create with this request_id may have reached Apple; refusing a duplicate",
            )
        return str(row[1])

    def begin_idempotency(self, request_id: str, request_hash: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO idempotency VALUES (?, ?, NULL, 'pending', ?)",
                (request_id, request_hash, int(time.time())),
            )

    def put_idempotency(self, request_id: str, request_hash: str, reminder_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE idempotency SET reminder_id=?, status='complete' "
                "WHERE request_id=? AND request_hash=?",
                (reminder_id, request_id, request_hash),
            )

    def consume_nonce(self, nonce: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT used_at FROM confirmations WHERE nonce=?", (nonce,)).fetchone()
            if row and row[0] is not None:
                raise AppError("CONFIRMATION_ALREADY_USED", "Confirmation token was already used")
            if row:
                db.execute(
                    "UPDATE confirmations SET used_at=? WHERE nonce=?", (int(time.time()), nonce)
                )
            else:
                db.execute("INSERT INTO confirmations VALUES (?, ?)", (nonce, int(time.time())))


class ConfirmationTokens:
    def __init__(self, secret: str, ttl: int, store: StateStore):
        if len(secret) < 32:
            raise AppError(
                "INVALID_CONFIGURATION",
                "CONFIRMATION_SIGNING_SECRET must contain at least 32 characters",
            )
        self.secret = secret.encode()
        self.ttl = ttl
        self.store = store

    def prepare(self, reminder_id: str, list_id: str, revision: str | None) -> str:
        import base64

        body = {
            "op": "delete_reminder",
            "rid": reminder_id,
            "lid": list_id,
            "rev": revision,
            "exp": int(time.time()) + self.ttl,
            "nonce": os.urandom(16).hex(),
        }
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + sig).decode().rstrip("=")

    def consume(self, token: str) -> dict[str, Any]:
        import base64

        try:
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            raw, sig = decoded[:-32], decoded[-32:]
            if not hmac.compare_digest(sig, hmac.new(self.secret, raw, hashlib.sha256).digest()):
                raise ValueError
            body = json.loads(raw)
        except Exception as exc:
            raise AppError("CONFIRMATION_INVALID", "Confirmation token is invalid") from exc
        if body.get("op") != "delete_reminder":
            raise AppError("CONFIRMATION_INVALID", "Confirmation token operation is invalid")
        if int(body.get("exp", 0)) < int(time.time()):
            raise AppError("CONFIRMATION_EXPIRED", "Confirmation token has expired")
        self.store.consume_nonce(str(body["nonce"]))
        return body


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
