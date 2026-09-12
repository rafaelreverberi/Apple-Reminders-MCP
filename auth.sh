#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
ACTION="${1:-status}"
[ -f "$ENV_FILE" ] || { printf 'Error: run ./setup.sh first.\n' >&2; exit 1; }
set -a
. "$ENV_FILE"
set +a
: "${ICLOUD_SESSION_DIR:=state/pyicloud-session}"
case "$ICLOUD_SESSION_DIR" in /*) SESSION_DIR="$ICLOUD_SESSION_DIR" ;; *) SESSION_DIR="$ROOT_DIR/$ICLOUD_SESSION_DIR" ;; esac
[ -n "${ICLOUD_USERNAME:-}" ] || { printf 'Error: set ICLOUD_USERNAME in .env.\n' >&2; exit 1; }
[ -t 0 ] || [ "$ACTION" != login ] || { printf 'Error: login requires an interactive terminal.\n' >&2; exit 1; }
CLI="$ROOT_DIR/apple-reminders-mcp-server/.venv/bin/icloud"
[ -x "$CLI" ] || { printf 'Error: run ./setup.sh first.\n' >&2; exit 1; }
# Python.org macOS builds may not have a populated OpenSSL CA file. pyicloud's
# 2FA trusted-device bridge uses raw TLS sockets, so point it at certifi without
# disabling verification. Respect an explicit operator override.
if [ -z "${SSL_CERT_FILE:-}" ]; then
  SSL_CERT_FILE=$("$ROOT_DIR/apple-reminders-mcp-server/.venv/bin/python" -c 'import certifi; print(certifi.where())')
  export SSL_CERT_FILE
fi
: "${REQUESTS_CA_BUNDLE:=$SSL_CERT_FILE}"
export REQUESTS_CA_BUNDLE
COMMON=(--username "$ICLOUD_USERNAME" --session-dir "$SESSION_DIR")
CHINA=()
case "${ICLOUD_CHINA_MAINLAND:-false}" in true|1|yes|on) CHINA=(--china-mainland) ;; esac
case "$ACTION" in
  login)
    "$CLI" auth login "${COMMON[@]}" "${CHINA[@]}"
    "$ROOT_DIR/apple-reminders-mcp-server/.venv/bin/python" -m apple_reminders_mcp.auth_check
    printf '\nApple authentication successful.\n\nTrusted iCloud session: yes\nReminders service:      available\n\nYou may now run:\n./start.sh\n'
    ;;
  status) "$CLI" auth status "${COMMON[@]}" ;;
  doctor) "$CLI" doctor "${COMMON[@]}" ;;
  logout) "$CLI" auth logout "${COMMON[@]}" ;;
  *) printf 'Usage: ./auth.sh login|status|doctor|logout\n' >&2; exit 1 ;;
esac
