import time

from .auth import create_saved_session
from .config import Settings
from .errors import AppError


def main() -> None:
    last_code = "REMINDERS_SERVICE_UNAVAILABLE"
    # Immediately after a new trusted login, Apple's CloudKit service can take
    # a few seconds before its first Reminders query succeeds. Retry only this
    # harmless read and create a fresh client each time.
    for delay in (0, 2, 5):
        if delay:
            time.sleep(delay)
        try:
            api = create_saved_session(Settings.from_env())
            next(iter(api.reminders.lists()), None)
            return
        except AppError as exc:
            last_code = exc.code
        except Exception:
            last_code = "REMINDERS_SERVICE_UNAVAILABLE"
    raise SystemExit(f"Authentication verification failed: {last_code}")


if __name__ == "__main__":
    main()
