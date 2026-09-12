# Apple-Reminders-MCP

A private, Raspberry Pi–friendly MCP server for real Apple Reminders. It uses
`pyicloud` 2.7.0's typed CloudKit-backed Reminders service, MCP Python SDK 2.2.0
Streamable HTTP on loopback, and OpenAI Secure MCP Tunnel. It is not CalDAV,
EventKit, AppleScript, browser automation, or a public reverse proxy.

> [!IMPORTANT]
> This is an unofficial Apple integration. Apple provides no stable public
> Reminders API for this use case. Apple can change its private web services and
> temporarily break pyicloud or this server.

## Architecture

```text
Apple Reminders <-> iCloud private CloudKit APIs <-> pyicloud
  <-> Reminders service <-> MCP 127.0.0.1:13003/mcp
  <-> tunnel-client outbound HTTPS <-> OpenAI <-> ChatGPT/Codex
```

The Pi writes to iCloud; Apple's normal sync delivers changes to iPhone, iPad,
Mac, and Watch. No Mac is required after setup and no inbound port is opened.

## Tools

Read tools: `health_check`, `check_session_status`, `list_reminder_lists`,
`list_reminders`, `search_reminders`, `get_reminder`, `list_subtasks`,
`get_reminder_recurrence`, `list_reminder_tags`, `list_reminder_attachments`, and
`list_reminder_alarms`.

Write tools: `create_reminder`, `update_reminder`, `set_reminder_completed`,
`create_subtask`, `set_reminder_recurrence`, `clear_reminder_recurrence`,
`add_reminder_tag`, `remove_reminder_tag`, `add_reminder_url_attachment`,
`remove_reminder_attachment`, and `add_location_reminder`.

Deletion uses `prepare_delete_reminder` then `confirm_delete_reminder`. Location
trigger removal is intentionally absent because pyicloud 2.7.0 has no reliable
public removal method. Binary attachments are metadata-only and never downloaded.

Apple implements Reminder deletion as a CloudKit soft delete (`Deleted = 1`),
which is what the Reminders UI presents in Recently Deleted. After Apple's modify
acknowledgement, the MCP verifies that the exact record is absent, marked deleted,
or no longer active in its original list. A briefly stale active result is retried
three times with a short bounded delay; an entry that remains active in the
original list returns `UNKNOWN_REMOTE_STATE`.

## Raspberry Pi setup

Requirements: Raspberry Pi OS 64-bit or Debian-compatible Linux, Python 3.11+,
`uv`, `curl`, `openssl`, and the current ARM64 `tunnel-client`.

```bash
git clone https://github.com/rafaelreverberi/Apple-Reminders-MCP.git
cd Apple-Reminders-MCP
./setup.sh
nano .env
```

Fill these values:

```env
ICLOUD_USERNAME=you@example.com
CONFIRMATION_SIGNING_SECRET=<output of openssl rand -base64 48>
CONTROL_PLANE_API_KEY=<OpenAI tunnel runtime API key>
CONTROL_PLANE_TUNNEL_ID=tunnel_...
```

Keep `MCP_HOST=127.0.0.1`. Optionally set stable list IDs in
`ALLOWED_REMINDER_LISTS`, `WRITABLE_REMINDER_LISTS`, and
`DEFAULT_REMINDER_LIST`. Empty allowlists mean all account lists; a writable
allowlist restricts mutation independently. Duplicate configured titles fail
closed, so IDs are preferred.

## Apple login and session persistence

```bash
./auth.sh login
```

The current pyicloud CLI securely prompts for the normal Apple Account password
and 2FA code in the SSH terminal. Neither value is accepted by MCP tools, stored
in `.env`, passed on the command line, or logged. pyicloud may offer to store the
password in the system keyring. Session-only mode is supported; do not install a
plaintext keyring backend. On a headless Pi, use a properly configured encrypted
system keyring only if you understand its unlock behavior.

The supported pyicloud `--session-dir` option persists cookies and session state
under `state/pyicloud-session` (0600/0700 protections). Authentication and the
systemd service must run as the same normal OS user.

On macOS, the scripts automatically point Python's raw TLS sockets at the locked
`certifi` CA bundle. This handles Python.org installations whose OpenSSL default
CA file is empty; certificate verification remains enabled. As a system-wide
alternative, Python.org also installs `/Applications/Python 3.x/Install Certificates.command`.

```bash
./auth.sh status
./auth.sh doctor
./auth.sh logout
```

If Apple requires updated legal terms, inspect `./auth.sh doctor` and consciously
run the pyicloud CLI's `auth login ... --accept-terms` operator flow. The MCP never
accepts legal terms.

## Start and health

```bash
./start.sh
curl http://127.0.0.1:13003/health
./stop.sh
```

The HTTP process stays alive in a degraded state when authentication is missing.
Run `./auth.sh login` and restart. It never prompts, loops on 2FA, or asks ChatGPT
for credentials. Naive timestamps use `Europe/Zurich`; aware values are converted
with `zoneinfo`, including CET/CEST and DST transitions.

All-lists reads retry transient iCloud failures once during service/list discovery
and once per list. If one list still fails, reminders from the other lists are
returned in the unchanged list response; if discovery or every list fails after
retry, the request returns the appropriate sanitized MCP error. iCloud calls that
do not provide their own timeout use a 10-second connect and 60-second read timeout.
Operational logs record the failing stage, attempt, list index, an opaque hashed
list reference, and the server-side traceback. Malformed individual records are
logged and skipped without discarding valid records from the same batch.

## MCP and tunnel testing

Use MCP Inspector with Streamable HTTP URL `http://127.0.0.1:13003/mcp`, or follow
[SECURE_TUNNEL_SETUP.md](SECURE_TUNNEL_SETUP.md). After a tool schema change,
rescan/refresh tools in the ChatGPT developer-mode app.

## systemd

```bash
./autostart.sh setup
./autostart.sh status
./autostart.sh logs
./autostart.sh restart
./autostart.sh remove
```

The generated service uses the current non-root user and its `HOME`, waits for
network-online, starts the MCP plus tunnel, and backs off actual crashes. An
expired Apple session is degraded health, not a process crash or restart loop.

## Security and operations

- `.env` must be mode 0600; runtime state is ignored by Git.
- The MCP rejects non-loopback hosts and the tunnel uses outbound HTTPS only.
- Reminder content and attachment/location fields are untrusted data, never commands.
- Writes require exact reminder IDs; fuzzy search only returns candidates.
- Create supports `request_id` idempotency without storing full Reminder content.
- Delete tokens are HMAC-signed, expire, are single-use, and bind ID, list, operation,
  and current CloudKit revision. Parent deletion is blocked while subtasks exist.
- Read/write limits are enforced in-process. Logs contain no Reminder payloads;
  list and record context is represented by short one-way hashes.

Read [SECURITY.md](SECURITY.md) before enabling writes.

## Development

```bash
uv sync --directory apple-reminders-mcp-server --locked --all-extras
apple-reminders-mcp-server/.venv/bin/ruff check apple-reminders-mcp-server/src tests
apple-reminders-mcp-server/.venv/bin/pytest tests
bash -n setup.sh auth.sh start.sh stop.sh autostart.sh
```

Mocked tests never need Apple/OpenAI credentials. The optional real-account test
is off by default and targets only `ICLOUD_TEST_REMINDER_LIST_ID`:

```bash
RUN_ICLOUD_INTEGRATION_TESTS=true \
ICLOUD_TEST_REMINDER_LIST_ID=<stable-test-list-id> \
apple-reminders-mcp-server/.venv/bin/pytest tests/test_live_icloud.py -s
```

It creates, updates, completes/reopens, tags, attaches a URL, recurs, and deletes
one generated Reminder. If cleanup cannot be confirmed it prints only the exact
opaque Reminder ID for manual cleanup.

## Known limitations

Private Apple APIs can change; sessions expire; iCloud may rate-limit or require
new terms. URL attachment and location-trigger writes follow pyicloud 2.7.0 but
must still be validated against a designated real test list. Incremental sync is
not the sole read path and is not currently cached, favoring correctness over a
stale duplicate database. Exact remote outcomes can be unknowable after a network
loss; the server does not report fake success.

MIT licensed.
