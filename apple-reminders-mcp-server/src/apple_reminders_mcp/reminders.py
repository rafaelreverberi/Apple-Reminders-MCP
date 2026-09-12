from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from typing import Any

from pyicloud.services.reminders.client import RemindersApiError, RemindersAuthError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from .auth import create_saved_session
from .config import Settings
from .errors import (
    AppError,
    classify_icloud_error,
    exception_chain,
    is_icloud_data_access_approval_error,
)
from .normalization import (
    iso,
    parse_datetime,
    priority_value,
    reminder_payload,
    validate_url,
)
from .security import ConfirmationTokens, RateLimiter, StateStore, request_hash

LOGGER = logging.getLogger(__name__)
_READ_ATTEMPTS = 2
_READ_RETRY_DELAY_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 30.0
_DELETE_VERIFY_ATTEMPTS = 3
_DELETE_VERIFY_DELAY_SECONDS = 0.5


def _attr(item: Any, name: str, default: Any = None) -> Any:
    return getattr(item, name, default)


def _log_ref(value: Any) -> str:
    """Return a stable opaque reference without putting Apple IDs or titles in logs."""
    raw = str(value or "<missing>").encode("utf-8", errors="replace")
    return sha256(raw).hexdigest()[:12]


class RemindersService:
    def __init__(
        self,
        settings: Settings,
        service_factory: Callable[[Settings], Any] = create_saved_session,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.service_factory = service_factory
        self._api: Any | None = None
        self._reminders_api: Any | None = None
        self._sleep = sleep
        self.rate = RateLimiter(settings.read_rate, settings.write_rate)
        self.store = StateStore(settings.state_dir / "security.sqlite3")

    @property
    def api(self) -> Any:
        if self._api is None:
            self._api = self.service_factory(self.settings)
        return self._api

    @property
    def reminders(self) -> Any:
        if self._reminders_api is not None:
            return self._reminders_api
        try:
            self._reminders_api = self.api.reminders
            return self._reminders_api
        except AppError:
            raise
        except Exception as exc:
            self._raise_remote(exc)

    def reset(self) -> None:
        self._api = None
        self._reminders_api = None

    @staticmethod
    def _exception_chain(exc: BaseException) -> list[BaseException]:
        return exception_chain(exc)

    @classmethod
    def _is_auth_error(cls, exc: BaseException) -> bool:
        return any(isinstance(item, RemindersAuthError) for item in cls._exception_chain(exc))

    @classmethod
    def _is_transient_read_error(cls, exc: BaseException) -> bool:
        transient_text = (
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "rate limit",
            "retry_after",
            "service_unavailable",
            "service unavailable",
            "temporar",
            "timed out",
            "timeout",
            "try again",
            "zone busy",
        )
        for item in cls._exception_chain(exc):
            if isinstance(
                item,
                (
                    RemindersAuthError,
                    RequestsConnectionError,
                    RequestsTimeout,
                    ConnectionError,
                    TimeoutError,
                ),
            ):
                return True
            if isinstance(item, RemindersApiError):
                details = f"{item} {getattr(item, 'payload', None)!r}".lower()
                if any(marker in details for marker in transient_text):
                    return True
        return False

    @classmethod
    def _retry_delay(cls, exc: BaseException) -> float:
        for item in cls._exception_chain(exc):
            payload = getattr(item, "payload", None)
            if not isinstance(payload, dict) or "retry_after" not in payload:
                continue
            try:
                return min(
                    _MAX_RETRY_AFTER_SECONDS,
                    max(_READ_RETRY_DELAY_SECONDS, float(payload["retry_after"])),
                )
            except (TypeError, ValueError):
                break
        return _READ_RETRY_DELAY_SECONDS

    def _fetch_list_batch(
        self,
        target: Any,
        *,
        include_completed: bool,
        results_limit: int,
        request_scope: str,
        list_index: int,
    ) -> Any:
        list_ref = _log_ref(_attr(target, "id"))
        for attempt in range(1, _READ_ATTEMPTS + 1):
            try:
                return self.reminders.list_reminders(
                    target.id,
                    include_completed=include_completed,
                    results_limit=results_limit,
                )
            except Exception as exc:
                transient = self._is_transient_read_error(exc)
                log = LOGGER.error if is_icloud_data_access_approval_error(exc) else LOGGER.exception
                log(
                    "Reminders read failed stage=fetch_list scope=%s list_index=%d "
                    "list_ref=%s attempt=%d/%d transient=%s exception_type=%s",
                    request_scope,
                    list_index,
                    list_ref,
                    attempt,
                    _READ_ATTEMPTS,
                    transient,
                    type(exc).__name__,
                )
                if not transient or attempt == _READ_ATTEMPTS:
                    raise
                if self._is_auth_error(exc):
                    self.reset()
                    LOGGER.warning(
                        "Reset cached iCloud client before read retry stage=session_refresh "
                        "scope=%s list_index=%d list_ref=%s",
                        request_scope,
                        list_index,
                        list_ref,
                    )
                delay = self._retry_delay(exc)
                LOGGER.warning(
                    "Retrying Reminders read stage=retry_wait scope=%s list_index=%d "
                    "list_ref=%s delay_seconds=%.1f",
                    request_scope,
                    list_index,
                    list_ref,
                    delay,
                )
                self._sleep(delay)
        raise AssertionError("unreachable")

    @classmethod
    def _raise_remote(cls, exc: Exception) -> None:
        raise classify_icloud_error(exc, include_not_found=True) from exc

    def _lists(self) -> list[Any]:
        for attempt in range(1, _READ_ATTEMPTS + 1):
            try:
                return [
                    item for item in self.reminders.lists() if not _attr(item, "deleted", False)
                ]
            except Exception as exc:
                # Accessing ``self.reminders`` can itself fail while pyicloud
                # refreshes PCS data. That error is already an AppError here,
                # but its cause chain still contains the transient transport
                # exception and must remain eligible for this read retry.
                if isinstance(exc, AppError) and exc.__cause__ is None:
                    raise
                transient = self._is_transient_read_error(exc)
                log = LOGGER.error if is_icloud_data_access_approval_error(exc) else LOGGER.exception
                log(
                    "Reminders read failed stage=list_discovery attempt=%d/%d transient=%s "
                    "exception_type=%s",
                    attempt,
                    _READ_ATTEMPTS,
                    transient,
                    type(exc).__name__,
                )
                if not transient or attempt == _READ_ATTEMPTS:
                    if isinstance(exc, AppError):
                        raise
                    self._raise_remote(exc)
                if self._is_auth_error(exc):
                    self.reset()
                    LOGGER.warning(
                        "Reset cached iCloud client before read retry stage=session_refresh "
                        "scope=list_discovery"
                    )
                delay = self._retry_delay(exc)
                LOGGER.warning(
                    "Retrying Reminders read stage=retry_wait scope=list_discovery "
                    "delay_seconds=%.1f",
                    delay,
                )
                self._sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _matches(selectors: tuple[str, ...], item: Any) -> bool:
        return (
            not selectors
            or item.id in selectors
            or item.title.casefold() in {x.casefold() for x in selectors}
        )

    def _visible_lists(self) -> list[Any]:
        lists = self._lists()
        if not self.settings.allowed_lists:
            return lists
        result = [item for item in lists if self._matches(self.settings.allowed_lists, item)]
        for selector in self.settings.allowed_lists:
            titled = [item for item in lists if item.title.casefold() == selector.casefold()]
            if len(titled) > 1 and selector not in {item.id for item in lists}:
                raise AppError(
                    "AMBIGUOUS_REMINDER_LIST",
                    f"Configured list name {selector!r} is duplicated; use stable IDs",
                )
        return result

    def _resolve_list(self, selector: str | None, *, write: bool = False) -> Any:
        lists = self._visible_lists()
        selected = selector or (self.settings.default_list if write else None)
        if selected:
            by_id = [item for item in lists if item.id == selected]
            matches = by_id or [
                item for item in lists if item.title.casefold() == selected.casefold()
            ]
            if len(matches) > 1:
                raise AppError(
                    "AMBIGUOUS_REMINDER_LIST",
                    "More than one list has that title; use a stable list ID",
                )
            if not matches:
                raise AppError(
                    "REMINDER_LIST_NOT_FOUND", "Reminder list was not found or is not allowed"
                )
            item = matches[0]
        elif write:
            writable = [item for item in lists if self._write_allowed(item)]
            if len(writable) != 1:
                raise AppError(
                    "REMINDER_LIST_NOT_FOUND",
                    "Select an exact list_id; no unambiguous default is configured",
                )
            item = writable[0]
        else:
            raise AppError("REMINDER_LIST_NOT_FOUND", "Select a reminder list")
        if write and not self._write_allowed(item):
            raise AppError("WRITE_NOT_ALLOWED", "Writes are not allowed for this reminder list")
        return item

    def _write_allowed(self, item: Any) -> bool:
        selectors = self.settings.writable_lists
        return self._matches(selectors, item) if selectors else True

    def _ensure_write(self, *, delete: bool = False) -> None:
        self.rate.check("write")
        if not self.settings.writes_enabled:
            raise AppError("WRITE_ACTIONS_DISABLED", "Write actions are disabled by configuration")
        if delete and not self.settings.deletes_enabled:
            raise AppError(
                "DELETE_ACTIONS_DISABLED", "Delete actions are disabled by configuration"
            )

    def _get(self, reminder_id: str, *, write: bool = False) -> Any:
        if not reminder_id.strip():
            raise AppError("REMINDER_NOT_FOUND", "An exact reminder_id is required")
        try:
            item = self.reminders.get(reminder_id)
        except Exception as exc:
            self._raise_remote(exc)
        target = self._resolve_list(item.list_id, write=write)
        if target.id != item.list_id:
            raise AppError("READ_NOT_ALLOWED", "Reminder is outside the allowed lists")
        return item

    def _rich(self, item: Any) -> dict[str, Any]:
        try:
            tags = [self._tag(t) for t in self.reminders.tags_for(item)]
            attachments = [self._attachment(a) for a in self.reminders.attachments_for(item)]
            recurrence = [self._recurrence(r) for r in self.reminders.recurrence_rules_for(item)]
            alarms = [self._alarm(a) for a in self.reminders.alarms_for(item)]
        except Exception:
            tags, attachments, recurrence, alarms = [], [], [], []
        return reminder_payload(
            item,
            self.settings.timezone,
            rich={
                "hashtags": tags,
                "attachments": attachments,
                "recurrence": recurrence,
                "alarms": alarms,
            },
        )

    def list_lists(self) -> list[dict[str, Any]]:
        self.rate.check("read")
        return [
            {
                "list_id": x.id,
                "title": x.title,
                "color": x.color,
                "count": x.count,
                "is_group": x.is_group,
                "read_allowed": True,
                "write_allowed": self._write_allowed(x),
                "is_default": self.settings.default_list in {x.id, x.title},
            }
            for x in self._visible_lists()
        ]

    def list_items(
        self,
        list_id: str | None = None,
        include_completed: bool = False,
        limit: int = 100,
        due_after: str | None = None,
        due_before: str | None = None,
        flagged: bool | None = None,
        priority: str | int | None = None,
        parent_reminder_id: str | None = None,
        *,
        _search_needle: str | None = None,
    ) -> list[dict[str, Any]]:
        self.rate.check("read")
        limit = min(max(1, limit), self.settings.max_reminders)
        lists = [self._resolve_list(list_id)] if list_id else self._visible_lists()
        after = parse_datetime(due_after, self.settings.timezone)
        before = parse_datetime(due_before, self.settings.timezone)
        pval = priority_value(priority, optional=True)
        results = []
        remaining = self.settings.max_reminders
        request_scope = "single_list" if list_id else "all_lists"
        failed_lists: list[Exception] = []
        successful_lists = 0
        for list_index, target in enumerate(lists):
            try:
                batch = self._fetch_list_batch(
                    target,
                    include_completed=include_completed,
                    results_limit=remaining,
                    request_scope=request_scope,
                    list_index=list_index,
                )
            except Exception as exc:
                if list_id is not None:
                    self._raise_remote(exc)
                failed_lists.append(exc)
                LOGGER.error(
                    "Skipping failed list in all-lists read stage=isolate_list "
                    "list_index=%d list_ref=%s",
                    list_index,
                    _log_ref(_attr(target, "id")),
                )
                continue

            try:
                batch_items = list(batch.reminders)
            except Exception as exc:
                LOGGER.exception(
                    "Reminders read failed stage=decode_list_batch scope=%s list_index=%d "
                    "list_ref=%s",
                    request_scope,
                    list_index,
                    _log_ref(_attr(target, "id")),
                )
                if list_id is not None:
                    self._raise_remote(exc)
                failed_lists.append(exc)
                continue

            hashtags_by_reminder: dict[str, list[str]] = {}
            if _search_needle is not None:
                batch_hashtags = _attr(batch, "hashtags", {})
                if isinstance(batch_hashtags, dict):
                    for hashtag in batch_hashtags.values():
                        reminder_id = _attr(hashtag, "reminder_id")
                        name = _attr(hashtag, "name")
                        if reminder_id and name:
                            hashtags_by_reminder.setdefault(str(reminder_id), []).append(str(name))

            successful_lists += 1
            for record_index, item in enumerate(batch_items):
                try:
                    due = _attr(item, "due_date")
                    if after and (not due or due < after):
                        continue
                    if before and (not due or due > before):
                        continue
                    if flagged is not None and bool(_attr(item, "flagged", False)) != flagged:
                        continue
                    if pval is not None and int(_attr(item, "priority", 0)) != pval:
                        continue
                    if (
                        parent_reminder_id is not None
                        and _attr(item, "parent_reminder_id") != parent_reminder_id
                    ):
                        continue
                    if _search_needle is not None:
                        searchable = "\n".join(
                            (
                                str(_attr(item, "title", "") or ""),
                                str(_attr(item, "desc", "") or ""),
                                " ".join(hashtags_by_reminder.get(str(_attr(item, "id")), [])),
                            )
                        )
                        if _search_needle not in searchable.casefold():
                            continue
                    results.append(reminder_payload(item, self.settings.timezone))
                except Exception:
                    LOGGER.exception(
                        "Skipping malformed reminder stage=normalize_record list_index=%d "
                        "list_ref=%s record_index=%d record_ref=%s",
                        list_index,
                        _log_ref(_attr(target, "id")),
                        record_index,
                        _log_ref(_attr(item, "id")),
                    )
                    continue
                if len(results) >= limit:
                    if failed_lists:
                        LOGGER.warning(
                            "Returning partial all-lists result stage=partial_result "
                            "successful_lists=%d failed_lists=%d result_count=%d",
                            successful_lists,
                            len(failed_lists),
                            len(results),
                        )
                    return results
            remaining = max(1, self.settings.max_reminders - len(results))
        if failed_lists and successful_lists == 0:
            self._raise_remote(failed_lists[-1])
        if failed_lists:
            LOGGER.warning(
                "Returning partial all-lists result stage=partial_result "
                "successful_lists=%d failed_lists=%d result_count=%d",
                successful_lists,
                len(failed_lists),
                len(results),
            )
        return results

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        if not needle:
            raise AppError("INVALID_QUERY", "Search query must not be empty")
        return self.list_items(_search_needle=needle, **filters)

    def get_item(self, reminder_id: str) -> dict[str, Any]:
        self.rate.check("read")
        return self._rich(self._get(reminder_id))

    def create(
        self,
        title: str,
        list_id: str | None = None,
        description: str = "",
        due: str | None = None,
        priority: str | int = "none",
        flagged: bool = False,
        all_day: bool = False,
        timezone: str | None = None,
        parent_reminder_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_write()
        if not title.strip() or len(title) > 1000:
            raise AppError("INVALID_TITLE", "Title must contain 1 to 1000 characters")
        target = self._resolve_list(list_id, write=True)
        if parent_reminder_id:
            parent = self._get(parent_reminder_id, write=True)
            if parent.list_id != target.id:
                raise AppError("WRITE_NOT_ALLOWED", "A subtask must use its parent's list")
        zone = timezone or self.settings.timezone
        payload = {
            "title": title.strip(),
            "list_id": target.id,
            "description": description,
            "due": due,
            "priority": priority,
            "flagged": flagged,
            "all_day": all_day,
            "timezone": zone,
            "parent_reminder_id": parent_reminder_id,
        }
        digest = request_hash(payload)
        if request_id:
            existing = self.store.get_idempotency(request_id, digest)
            if existing:
                return {**self.get_item(existing), "idempotent_replay": True}
            try:
                self.store.begin_idempotency(request_id, digest)
            except Exception as exc:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT", "request_id is already being processed"
                ) from exc
        try:
            item = self.reminders.create(
                target.id,
                title.strip(),
                desc=description,
                due_date=parse_datetime(due, zone),
                priority=priority_value(priority),
                flagged=flagged,
                all_day=all_day,
                time_zone=zone if due else None,
                parent_reminder_id=parent_reminder_id,
            )
        except Exception as exc:
            self._raise_remote(exc)
        if request_id:
            self.store.put_idempotency(request_id, digest, item.id)
        return self.get_item(item.id)

    def update(
        self,
        reminder_id: str,
        title: str | None = None,
        description: str | None = None,
        due: str | None = None,
        clear_due: bool = False,
        priority: str | int | None = None,
        flagged: bool | None = None,
        all_day: bool | None = None,
    ) -> dict[str, Any]:
        self._ensure_write()
        if due is not None and clear_due:
            raise AppError("INVALID_DUE_DATE", "due and clear_due cannot be used together")
        item = self._get(reminder_id, write=True)
        if title is not None:
            if not title.strip():
                raise AppError("INVALID_TITLE", "Title must not be empty")
            item.title = title.strip()
        if description is not None:
            item.desc = description
        if due is not None:
            item.due_date = parse_datetime(due, self.settings.timezone)
            item.time_zone = self.settings.timezone
        elif clear_due:
            item.due_date = None
            item.time_zone = None
        if priority is not None:
            item.priority = priority_value(priority)
        if flagged is not None:
            item.flagged = flagged
        if all_day is not None:
            item.all_day = all_day
        try:
            self.reminders.update(item)
        except Exception as exc:
            self._raise_remote(exc)
        return self.get_item(reminder_id)

    def complete(self, reminder_id: str, completed: bool) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        from zoneinfo import ZoneInfo

        item.completed = completed
        item.completed_date = datetime.now(ZoneInfo(self.settings.timezone)) if completed else None
        try:
            self.reminders.update(item)
        except Exception as exc:
            self._raise_remote(exc)
        return self.get_item(reminder_id)

    def subtasks(
        self, parent_id: str, include_completed: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        parent = self._get(parent_id)
        return self.list_items(
            parent.list_id, include_completed, limit, parent_reminder_id=parent_id
        )

    def _recurrence(self, rule: Any) -> dict[str, Any]:
        frequency = _attr(_attr(rule, "frequency"), "name", str(_attr(rule, "frequency"))).lower()
        return {
            "recurrence_id": rule.id,
            "reminder_id": rule.reminder_id,
            "frequency": frequency,
            "interval": rule.interval,
            "occurrence_count": rule.occurrence_count,
            "first_day_of_week": rule.first_day_of_week,
        }

    def recurrence(self, reminder_id: str) -> list[dict[str, Any]]:
        self.rate.check("read")
        item = self._get(reminder_id)
        return [self._recurrence(x) for x in self.reminders.recurrence_rules_for(item)]

    def set_recurrence(
        self,
        reminder_id: str,
        frequency: str,
        interval: int = 1,
        occurrence_count: int = 0,
        first_day_of_week: int = 0,
    ) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        from pyicloud.services.reminders.models import RecurrenceFrequency

        choices = {
            "daily": RecurrenceFrequency.DAILY,
            "weekly": RecurrenceFrequency.WEEKLY,
            "monthly": RecurrenceFrequency.MONTHLY,
            "yearly": RecurrenceFrequency.YEARLY,
        }
        if (
            frequency.lower() not in choices
            or interval < 1
            or occurrence_count < 0
            or not 0 <= first_day_of_week <= 6
        ):
            raise AppError(
                "INVALID_RECURRENCE",
                "Use daily/weekly/monthly/yearly, interval >= 1, occurrence_count >= 0, first_day_of_week 0..6",
            )
        rules = list(self.reminders.recurrence_rules_for(item))
        if len(rules) > 1:
            raise AppError(
                "REMOTE_CONFLICT", "Multiple recurrence rules exist; refusing to overwrite"
            )
        if rules:
            rule = rules[0]
            self.reminders.update_recurrence_rule(
                rule,
                frequency=choices[frequency.lower()],
                interval=interval,
                occurrence_count=occurrence_count,
                first_day_of_week=first_day_of_week,
            )
        else:
            rule = self.reminders.create_recurrence_rule(
                item,
                frequency=choices[frequency.lower()],
                interval=interval,
                occurrence_count=occurrence_count,
                first_day_of_week=first_day_of_week,
            )
        return self._recurrence(rule)

    def clear_recurrence(self, reminder_id: str) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        rules = list(self.reminders.recurrence_rules_for(item))
        if len(rules) > 1:
            raise AppError(
                "REMOTE_CONFLICT", "Multiple recurrence rules exist; refusing bulk removal"
            )
        for rule in rules:
            self.reminders.delete_recurrence_rule(item, rule)
        return {"reminder_id": reminder_id, "cleared": bool(rules)}

    def _tag(self, tag: Any) -> dict[str, Any]:
        return {
            "tag_id": tag.id,
            "name": tag.name,
            "created": iso(_attr(tag, "created"), self.settings.timezone),
        }

    def list_tags(self, reminder_id: str) -> list[dict[str, Any]]:
        self.rate.check("read")
        item = self._get(reminder_id)
        return [self._tag(x) for x in self.reminders.tags_for(item)]

    def add_tag(self, reminder_id: str, name: str) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        normalized = name.strip().lstrip("#").strip()
        if not normalized:
            raise AppError("INVALID_TAG", "Tag name must not be empty")
        for tag in self.reminders.tags_for(item):
            if tag.name.casefold() == normalized.casefold():
                return {"created": False, "tag": self._tag(tag)}
        return {"created": True, "tag": self._tag(self.reminders.create_hashtag(item, normalized))}

    def remove_tag(self, reminder_id: str, selector: str) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        selector = selector.strip().lstrip("#")
        tags = list(self.reminders.tags_for(item))
        matches = [x for x in tags if x.id == selector] or [
            x for x in tags if x.name.casefold() == selector.casefold()
        ]
        if not matches:
            raise AppError("TAG_NOT_FOUND", "Tag was not found")
        if len(matches) > 1:
            raise AppError("REMOTE_CONFLICT", "Tag name is ambiguous; use tag_id")
        self.reminders.delete_hashtag(item, matches[0])
        return {"removed": True, "tag": self._tag(matches[0])}

    def _attachment(self, item: Any) -> dict[str, Any]:
        result = {
            "attachment_id": item.id,
            "type": "url" if hasattr(item, "url") else "image",
            "uti": _attr(item, "uti"),
        }
        if hasattr(item, "url"):
            result["url"] = item.url
        else:
            result.update(
                {"filename": _attr(item, "filename"), "file_size": _attr(item, "file_size")}
            )
        return result

    def attachments(self, reminder_id: str) -> list[dict[str, Any]]:
        self.rate.check("read")
        return [self._attachment(x) for x in self.reminders.attachments_for(self._get(reminder_id))]

    def add_url(self, reminder_id: str, url: str) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        return self._attachment(self.reminders.create_url_attachment(item, validate_url(url)))

    def remove_attachment(self, reminder_id: str, attachment_id: str) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        matches = [x for x in self.reminders.attachments_for(item) if x.id == attachment_id]
        if not matches:
            raise AppError("ATTACHMENT_NOT_FOUND", "Attachment was not found")
        snapshot = self._attachment(matches[0])
        self.reminders.delete_attachment(item, matches[0])
        return {"removed": True, "attachment": snapshot}

    def _alarm(self, row: Any) -> dict[str, Any]:
        alarm, trigger = (
            (row.alarm, row.trigger) if hasattr(row, "alarm") else (row, _attr(row, "trigger"))
        )
        result = {"alarm_id": alarm.id, "trigger_id": _attr(alarm, "trigger_id"), "type": None}
        if trigger:
            proximity = _attr(
                _attr(trigger, "proximity"), "name", str(_attr(trigger, "proximity"))
            ).lower()
            result.update(
                {
                    "type": "location",
                    "title": trigger.title,
                    "address": trigger.address,
                    "latitude": trigger.latitude,
                    "longitude": trigger.longitude,
                    "radius": trigger.radius,
                    "proximity": proximity,
                }
            )
        return result

    def alarms(self, reminder_id: str) -> list[dict[str, Any]]:
        self.rate.check("read")
        return [self._alarm(x) for x in self.reminders.alarms_for(self._get(reminder_id))]

    def add_location(
        self,
        reminder_id: str,
        title: str,
        address: str,
        latitude: float,
        longitude: float,
        radius: float = 100,
        proximity: str = "arriving",
    ) -> dict[str, Any]:
        self._ensure_write()
        item = self._get(reminder_id, write=True)
        from pyicloud.services.reminders.models import Proximity

        choices = {"arriving": Proximity.ARRIVING, "leaving": Proximity.LEAVING}
        if (
            proximity not in choices
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
            or not 1 <= radius <= 100000
        ):
            raise AppError("INVALID_LOCATION_TRIGGER", "Invalid coordinates, radius, or proximity")
        alarm, trigger = self.reminders.add_location_trigger(
            item,
            title=title,
            address=address,
            latitude=latitude,
            longitude=longitude,
            radius=radius,
            proximity=choices[proximity],
        )
        return self._alarm(type("AlarmRow", (), {"alarm": alarm, "trigger": trigger})())

    def prepare_delete(self, reminder_id: str) -> dict[str, Any]:
        self._ensure_write(delete=True)
        item = self._get(reminder_id, write=True)
        children = self.subtasks(reminder_id, True, self.settings.max_reminders)
        if children:
            raise AppError(
                "UNSUPPORTED_OPERATION",
                "Deleting a parent with subtasks is blocked because cascade behavior is not guaranteed",
                details={"subtask_count": len(children)},
            )
        token = ConfirmationTokens(
            self.settings.confirmation_secret, self.settings.confirmation_ttl, self.store
        ).prepare(item.id, item.list_id, _attr(item, "record_change_tag"))
        return {
            "operation": "delete_reminder",
            "preview": {
                "reminder_id": item.id,
                "title": item.title,
                "list_id": item.list_id,
                "due": reminder_payload(item, self.settings.timezone)["due"],
                "completed": item.completed,
                "subtask_count": 0,
                "recurrence": self.recurrence(item.id),
            },
            "confirmation_token": token,
            "expires_in_seconds": self.settings.confirmation_ttl,
        }

    def _verify_deleted(self, item: Any) -> bool:
        reminder_ref = _log_ref(_attr(item, "id"))
        original_list_id = _attr(item, "list_id")
        list_ref = _log_ref(original_list_id)
        original_revision = _attr(item, "record_change_tag")

        for attempt in range(1, _DELETE_VERIFY_ATTEMPTS + 1):
            lookup_item = None
            lookup_state = "error"
            try:
                lookup_item = self.reminders.get(item.id)
            except (KeyError, LookupError):
                LOGGER.info(
                    "Delete verification state stage=post_delete_lookup attempt=%d/%d "
                    "reminder_ref=%s original_list_ref=%s lookup_state=absent",
                    attempt,
                    _DELETE_VERIFY_ATTEMPTS,
                    reminder_ref,
                    list_ref,
                )
                return True
            except Exception as exc:
                transient = self._is_transient_read_error(exc)
                LOGGER.exception(
                    "Delete verification failed stage=post_delete_lookup attempt=%d/%d "
                    "reminder_ref=%s original_list_ref=%s transient=%s",
                    attempt,
                    _DELETE_VERIFY_ATTEMPTS,
                    reminder_ref,
                    list_ref,
                    transient,
                )
                if self._is_auth_error(exc):
                    self.reset()
            else:
                lookup_deleted = bool(_attr(lookup_item, "deleted", False))
                lookup_list_matches = _attr(lookup_item, "list_id") == original_list_id
                lookup_revision_changed = (
                    _attr(lookup_item, "record_change_tag") != original_revision
                )
                lookup_state = "deleted" if lookup_deleted else "active"
                LOGGER.info(
                    "Delete verification state stage=post_delete_lookup attempt=%d/%d "
                    "reminder_ref=%s original_list_ref=%s lookup_state=%s "
                    "lookup_list_matches=%s revision_changed=%s",
                    attempt,
                    _DELETE_VERIFY_ATTEMPTS,
                    reminder_ref,
                    list_ref,
                    lookup_state,
                    lookup_list_matches,
                    lookup_revision_changed,
                )
                if lookup_deleted:
                    return True

            try:
                batch = self._fetch_list_batch(
                    type("DeleteVerificationList", (), {"id": original_list_id})(),
                    include_completed=True,
                    results_limit=self.settings.max_reminders,
                    request_scope="delete_verification",
                    list_index=0,
                )
                matches = [row for row in batch.reminders if _attr(row, "id") == item.id]
            except Exception as exc:
                transient = self._is_transient_read_error(exc)
                LOGGER.exception(
                    "Delete verification failed stage=post_delete_list_query attempt=%d/%d "
                    "reminder_ref=%s original_list_ref=%s transient=%s",
                    attempt,
                    _DELETE_VERIFY_ATTEMPTS,
                    reminder_ref,
                    list_ref,
                    transient,
                )
            else:
                active_matches = [row for row in matches if not _attr(row, "deleted", False)]
                LOGGER.info(
                    "Delete verification state stage=post_delete_list_query attempt=%d/%d "
                    "reminder_ref=%s original_list_ref=%s matching_records=%d "
                    "active_matching_records=%d lookup_state=%s",
                    attempt,
                    _DELETE_VERIFY_ATTEMPTS,
                    reminder_ref,
                    list_ref,
                    len(matches),
                    len(active_matches),
                    lookup_state,
                )
                if matches and not active_matches:
                    return True
                if not matches and (
                    lookup_item is None or _attr(lookup_item, "list_id") == original_list_id
                ):
                    # A stale point lookup can briefly return the old active
                    # object after the authoritative original-list query has
                    # already stopped returning it.
                    return True

            if attempt < _DELETE_VERIFY_ATTEMPTS:
                self._sleep(_DELETE_VERIFY_DELAY_SECONDS)

        LOGGER.error(
            "Delete verification remained ambiguous stage=post_delete_final "
            "reminder_ref=%s original_list_ref=%s attempts=%d",
            reminder_ref,
            list_ref,
            _DELETE_VERIFY_ATTEMPTS,
        )
        return False

    def confirm_delete(self, token: str) -> dict[str, Any]:
        self._ensure_write(delete=True)
        body = ConfirmationTokens(
            self.settings.confirmation_secret, self.settings.confirmation_ttl, self.store
        ).consume(token)
        item = self._get(body["rid"], write=True)
        if item.list_id != body["lid"] or _attr(item, "record_change_tag") != body.get("rev"):
            raise AppError("REMOTE_CONFLICT", "Reminder changed after deletion was prepared")
        try:
            self.reminders.delete(item)
        except Exception as exc:
            self._raise_remote(exc)
        LOGGER.info(
            "Delete modify acknowledged stage=delete_ack reminder_ref=%s original_list_ref=%s "
            "model_deleted=%s revision_changed=%s",
            _log_ref(item.id),
            _log_ref(item.list_id),
            bool(_attr(item, "deleted", False)),
            _attr(item, "record_change_tag") != body.get("rev"),
        )
        if self._verify_deleted(item):
            return {"deleted": True, "reminder_id": item.id}
        raise AppError("UNKNOWN_REMOTE_STATE", "Apple did not confirm that the reminder is gone")
