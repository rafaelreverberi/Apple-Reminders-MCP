from types import SimpleNamespace

import pytest
from apple_reminders_mcp.auth import sanitized_status
from apple_reminders_mcp.config import LOOPBACK_HOSTS, Settings
from apple_reminders_mcp.errors import AppError
from apple_reminders_mcp.normalization import (
    parse_datetime,
    priority_value,
    validate_url,
)
from apple_reminders_mcp.reminders import RemindersService
from pyicloud.services.reminders.client import RemindersApiError, RemindersAuthError
from requests.exceptions import ConnectionError as RequestsConnectionError


def reminder(**values):
    base = {
        "id": "r1",
        "list_id": "l1",
        "title": "Test",
        "desc": "notes",
        "completed": False,
        "completed_date": None,
        "due_date": None,
        "priority": 0,
        "flagged": False,
        "all_day": False,
        "time_zone": None,
        "parent_reminder_id": None,
        "created": None,
        "modified": None,
        "record_change_tag": "rev-1",
        "deleted": False,
    }
    base.update(values)
    return SimpleNamespace(**base)


class FakeReminders:
    def __init__(self):
        self.items = {"r1": reminder()}
        self.list_rows = [
            SimpleNamespace(
                id="l1",
                title="Work",
                color="#ff0000",
                count=1,
                is_group=False,
                deleted=False,
            ),
            SimpleNamespace(
                id="l2",
                title="Private",
                color=None,
                count=0,
                is_group=False,
                deleted=False,
            ),
        ]
        self.tags = {}
        self.attachments_map = {}
        self.rules = {}

    def lists(self):
        return self.list_rows

    def get(self, reminder_id):
        return self.items[reminder_id]

    def list_reminders(self, list_id, include_completed=False, results_limit=200):
        rows = [
            x
            for x in self.items.values()
            if x.list_id == list_id and (include_completed or not x.completed)
        ]
        return SimpleNamespace(reminders=rows[:results_limit])

    def create(self, list_id, title, **kwargs):
        item = reminder(
            id=f"r{len(self.items) + 1}",
            list_id=list_id,
            title=title,
            desc=kwargs["desc"],
            due_date=kwargs["due_date"],
            priority=kwargs["priority"],
            flagged=kwargs["flagged"],
            all_day=kwargs["all_day"],
            time_zone=kwargs["time_zone"],
            parent_reminder_id=kwargs["parent_reminder_id"],
            record_change_tag="new-rev",
        )
        self.items[item.id] = item
        return item

    def update(self, item):
        item.record_change_tag = item.record_change_tag

    def delete(self, item):
        del self.items[item.id]

    def tags_for(self, item):
        return self.tags.get(item.id, [])

    def create_hashtag(self, item, name):
        tag = SimpleNamespace(id=f"tag-{name}", name=name, created=None)
        self.tags.setdefault(item.id, []).append(tag)
        return tag

    def delete_hashtag(self, item, tag):
        self.tags[item.id].remove(tag)

    def attachments_for(self, item):
        return self.attachments_map.get(item.id, [])

    def create_url_attachment(self, item, url):
        row = SimpleNamespace(id="a1", url=url, uti="public.url")
        self.attachments_map.setdefault(item.id, []).append(row)
        return row

    def delete_attachment(self, item, attachment):
        self.attachments_map[item.id].remove(attachment)

    def recurrence_rules_for(self, item):
        return self.rules.get(item.id, [])

    def alarms_for(self, item):
        return []


class FakeApi:
    def __init__(self):
        self.reminders = FakeReminders()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        username="user@example.com",
        china_mainland=False,
        timezone="Europe/Zurich",
        default_list=None,
        allowed_lists=(),
        writable_lists=(),
        writes_enabled=True,
        deletes_enabled=True,
        host="127.0.0.1",
        port=13003,
        path="/mcp",
        session_dir=tmp_path / "session",
        state_dir=tmp_path / "state",
        confirmation_secret="x" * 48,
        confirmation_ttl=300,
        read_rate=1000,
        write_rate=1000,
        max_reminders=200,
    )


@pytest.fixture
def service(settings):
    api = FakeApi()
    return RemindersService(settings, service_factory=lambda _: api)


def test_loopback_and_environment(monkeypatch):
    assert LOOPBACK_HOSTS == {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    with pytest.raises(AppError, match="loopback"):
        Settings.from_env()


def test_zurich_dst_and_priority():
    winter = parse_datetime("2026-01-15T12:00:00", "Europe/Zurich")
    summer = parse_datetime("2026-07-15T12:00:00", "Europe/Zurich")
    assert winter.utcoffset().total_seconds() == 3600
    assert summer.utcoffset().total_seconds() == 7200
    assert priority_value("high") == 1
    assert priority_value(9) == 9


def test_url_validation_rejects_credentials():
    with pytest.raises(AppError):
        validate_url("https://user:secret@example.com")


def test_create_is_idempotent_and_patch_is_partial(service):
    first = service.create("New", "l1", due="2026-07-15T12:00:00", request_id="req-1")
    second = service.create("New", "l1", due="2026-07-15T12:00:00", request_id="req-1")
    assert first["reminder_id"] == second["reminder_id"]
    assert second["idempotent_replay"] is True
    result = service.update(first["reminder_id"], flagged=True)
    assert result["title"] == "New"
    assert result["flagged"] is True


def test_filters_subtasks_tags_and_attachments(service):
    child = service.create("Child", "l1", parent_reminder_id="r1")
    assert service.subtasks("r1")[0]["reminder_id"] == child["reminder_id"]
    assert service.add_tag("r1", "#School")["created"] is True
    assert service.add_tag("r1", "school")["created"] is False
    assert service.search("notes")[0]["reminder_id"] == "r1"
    attachment = service.add_url("r1", "https://example.com/page")
    assert attachment["type"] == "url"
    assert service.remove_attachment("r1", "a1")["removed"] is True


def test_prepare_delete_token_replay_and_revision_binding(service):
    prepared = service.prepare_delete("r1")
    result = service.confirm_delete(prepared["confirmation_token"])
    assert result == {"deleted": True, "reminder_id": "r1"}
    with pytest.raises(AppError) as exc:
        service.confirm_delete(prepared["confirmation_token"])
    assert exc.value.code == "CONFIRMATION_ALREADY_USED"


def test_duplicate_list_name_is_ambiguous(service):
    service.api.reminders.list_rows.append(
        SimpleNamespace(
            id="l3", title="Work", color=None, count=0, is_group=False, deleted=False
        )
    )
    with pytest.raises(AppError) as exc:
        service.create("No", "Work")
    assert exc.value.code == "AMBIGUOUS_REMINDER_LIST"


def test_session_status_valid_and_expired(settings):
    class Valid:
        reminders = object()

        def get_auth_status(self):
            return {
                "authenticated": True,
                "trusted_session": True,
                "requires_2fa": False,
                "requires_2sa": False,
            }

    valid = sanitized_status(settings, lambda _: Valid())
    assert valid["reminders_available"] is True
    assert valid["requires_reauthentication"] is False

    def expired(_settings):
        raise AppError("REAUTHENTICATION_REQUIRED", "expired")

    invalid = sanitized_status(settings, expired)
    assert invalid["requires_reauthentication"] is True
    assert "operator_hint" in invalid


def test_write_switch_and_list_allowlist(settings):
    api = FakeApi()
    restricted = Settings(
        **{**settings.__dict__, "allowed_lists": ("l1",), "writable_lists": ("l2",)}
    )
    svc = RemindersService(restricted, service_factory=lambda _: api)
    assert [row["list_id"] for row in svc.list_lists()] == ["l1"]
    with pytest.raises(AppError) as exc:
        svc.create("Blocked", "l1")
    assert exc.value.code == "WRITE_NOT_ALLOWED"

    disabled = Settings(**{**settings.__dict__, "writes_enabled": False})
    svc = RemindersService(disabled, service_factory=lambda _: api)
    with pytest.raises(AppError) as exc:
        svc.complete("r1", True)
    assert exc.value.code == "WRITE_ACTIONS_DISABLED"


def test_completion_and_reopening(service):
    assert service.complete("r1", True)["completed"] is True
    reopened = service.complete("r1", False)
    assert reopened["completed"] is False
    assert reopened["completed_date"] is None


def test_delete_parent_with_child_is_blocked(service):
    service.create("Child", "l1", parent_reminder_id="r1")
    with pytest.raises(AppError) as exc:
        service.prepare_delete("r1")
    assert exc.value.code == "UNSUPPORTED_OPERATION"


def test_delete_verification_accepts_immediate_disappearance(service):
    prepared = service.prepare_delete("r1")

    assert service.confirm_delete(prepared["confirmation_token"]) == {
        "deleted": True,
        "reminder_id": "r1",
    }


def test_delete_verification_accepts_recently_deleted_record(settings, caplog):
    class SoftDeletingReminders(FakeReminders):
        def delete(self, item):
            item.deleted = True
            item.record_change_tag = "deleted-revision"

    api = SimpleNamespace(reminders=SoftDeletingReminders())
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=lambda _: None)
    prepared = svc.prepare_delete("r1")

    with caplog.at_level("INFO"):
        result = svc.confirm_delete(prepared["confirmation_token"])

    assert result == {"deleted": True, "reminder_id": "r1"}
    assert "lookup_state=deleted" in caplog.text
    assert "Test" not in caplog.text


def test_delete_verification_retries_eventually_consistent_active_lookup(settings):
    class EventuallyDeletedReminders(FakeReminders):
        def __init__(self):
            super().__init__()
            self.delete_called = False
            self.post_delete_gets = 0

        def delete(self, item):
            self.delete_called = True

        def get(self, reminder_id):
            item = super().get(reminder_id)
            if self.delete_called:
                self.post_delete_gets += 1
                if self.post_delete_gets >= 2:
                    item.deleted = True
            return item

    api = SimpleNamespace(reminders=EventuallyDeletedReminders())
    delays = []
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=delays.append)
    prepared = svc.prepare_delete("r1")

    result = svc.confirm_delete(prepared["confirmation_token"])

    assert result == {"deleted": True, "reminder_id": "r1"}
    assert api.reminders.post_delete_gets == 2
    assert delays == [0.5]


def test_delete_verification_rejects_reminder_that_remains_active(settings, caplog):
    class FailedDeletingReminders(FakeReminders):
        def delete(self, item):
            return None

    api = SimpleNamespace(reminders=FailedDeletingReminders())
    delays = []
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=delays.append)
    prepared = svc.prepare_delete("r1")

    with caplog.at_level("INFO"), pytest.raises(AppError) as exc:
        svc.confirm_delete(prepared["confirmation_token"])

    assert exc.value.code == "UNKNOWN_REMOTE_STATE"
    assert delays == [0.5, 0.5]
    assert "active_matching_records=1" in caplog.text
    assert "stage=post_delete_final" in caplog.text


def test_all_lists_retries_and_isolates_one_failed_list(settings, caplog):
    class OneBrokenList(FakeReminders):
        def __init__(self):
            super().__init__()
            self.items["r2"] = reminder(id="r2", list_id="l2", title="Still returned")
            self.calls = []

        def list_reminders(self, list_id, include_completed=False, results_limit=200):
            self.calls.append(list_id)
            if list_id == "l1":
                raise RemindersApiError("HTTP 503")
            return super().list_reminders(list_id, include_completed, results_limit)

    api = SimpleNamespace(reminders=OneBrokenList())
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=lambda _: None)

    with caplog.at_level("WARNING"):
        result = svc.list_items(include_completed=False, limit=100)

    assert [item["reminder_id"] for item in result] == ["r2"]
    assert api.reminders.calls == ["l1", "l1", "l2"]
    assert "stage=fetch_list" in caplog.text
    assert "stage=isolate_list" in caplog.text
    assert "stage=partial_result" in caplog.text
    assert "HTTP 503" in caplog.text
    assert "list_ref=" in caplog.text


def test_all_lists_transient_failure_recovers_on_retry(settings):
    class FlakyList(FakeReminders):
        def __init__(self):
            super().__init__()
            self.failures = 0

        def list_reminders(self, list_id, include_completed=False, results_limit=200):
            if list_id == "l1" and self.failures == 0:
                self.failures += 1
                raise TimeoutError("temporary timeout")
            return super().list_reminders(list_id, include_completed, results_limit)

    api = SimpleNamespace(reminders=FlakyList())
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=lambda _: None)

    assert [item["reminder_id"] for item in svc.list_items()] == ["r1"]
    assert api.reminders.failures == 1


def test_list_discovery_retries_transient_pcs_service_initialization(settings, caplog):
    ready = FakeReminders()

    class FlakyPcsApi:
        attempts = 0

        @property
        def reminders(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RequestsConnectionError("remote disconnected during PCS refresh")
            return ready

    api = FlakyPcsApi()
    delays = []
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=delays.append)

    with caplog.at_level("ERROR"):
        result = svc.list_items()

    assert [item["reminder_id"] for item in result] == ["r1"]
    assert api.attempts == 2
    assert delays == [1.0]
    assert "stage=list_discovery" in caplog.text
    assert "transient=True" in caplog.text


def test_pyicloud_reminders_service_is_cached_across_all_list_fetches(settings):
    ready = FakeReminders()

    class CountingApi:
        accesses = 0

        @property
        def reminders(self):
            self.accesses += 1
            return ready

    api = CountingApi()
    svc = RemindersService(settings, service_factory=lambda _: api)

    assert [item["reminder_id"] for item in svc.list_items()] == ["r1"]
    assert api.accesses == 1


def test_auth_failure_refreshes_cached_session_before_retry(settings):
    class ExpiredReminders(FakeReminders):
        def list_reminders(self, list_id, include_completed=False, results_limit=200):
            raise RemindersAuthError("HTTP 401: unauthorized")

    apis = [SimpleNamespace(reminders=ExpiredReminders()), FakeApi()]
    created = []

    def factory(_settings):
        api = apis[len(created)]
        created.append(api)
        return api

    svc = RemindersService(settings, service_factory=factory, sleep=lambda _: None)

    assert [item["reminder_id"] for item in svc.list_items("l1")] == ["r1"]
    assert len(created) == 2


def test_malformed_record_does_not_discard_other_records(settings, caplog):
    api = FakeApi()
    api.reminders.items["bad"] = reminder(id="bad", priority="not-an-integer")
    svc = RemindersService(settings, service_factory=lambda _: api)

    with caplog.at_level("ERROR"):
        result = svc.list_items("l1")

    assert [item["reminder_id"] for item in result] == ["r1"]
    assert "stage=normalize_record" in caplog.text
    assert "ValueError" in caplog.text


def test_rate_limit_payload_is_classified_after_retries(settings):
    class RateLimited(FakeReminders):
        def list_reminders(self, list_id, include_completed=False, results_limit=200):
            raise RemindersApiError("request rejected", payload={"retry_after": 0})

    api = SimpleNamespace(reminders=RateLimited())
    svc = RemindersService(settings, service_factory=lambda _: api, sleep=lambda _: None)

    with pytest.raises(AppError) as exc:
        svc.list_items("l1")

    assert exc.value.code == "RATE_LIMITED"


def test_unexpected_mcp_error_is_logged_but_public_response_is_sanitized(monkeypatch, caplog):
    from apple_reminders_mcp import server as server_module

    class BrokenService:
        def list_items(self):
            raise RuntimeError("internal-diagnostic-marker")

    monkeypatch.setattr(server_module, "service", lambda: BrokenService())
    with caplog.at_level("ERROR"):
        response = server_module.invoke("list_items")

    assert response == {
        "ok": False,
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "The operation failed without exposing sensitive details",
        },
    }
    assert "internal-diagnostic-marker" not in str(response)
    assert "internal-diagnostic-marker" in caplog.text
    assert "Unexpected MCP operation failure operation=list_items" in caplog.text
