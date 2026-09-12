#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
for cmd in python3 uv curl openssl; do command -v "$cmd" >/dev/null 2>&1 || fail "required command not found: $cmd"; done
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || fail "Python 3.11+ is required"
if [ "$(uname -s)" = Linux ] && [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${ID_LIKE:-$ID}" in *debian*) ;; *) printf 'Warning: scripts are tested for Debian-compatible Linux.\n' ;; esac
fi
if [ ! -f .env ]; then cp .env.example .env; printf 'Created .env from .env.example.\n'; fi
chmod 600 .env
mkdir -p logs state .run state/pyicloud-session state/tunnel-profiles
chmod 700 state .run state/pyicloud-session state/tunnel-profiles
uv sync --directory apple-reminders-mcp-server --locked --all-extras
if ! command -v tunnel-client >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/tunnel-client" ]; then
  printf 'Warning: tunnel-client not found; install the current Raspberry Pi build before start.\n'
fi
printf '\nApple Reminders MCP setup complete.\n\nNext:\n1. Edit .env\n2. Set ICLOUD_USERNAME\n3. Generate CONFIRMATION_SIGNING_SECRET with: openssl rand -base64 48\n4. Set OpenAI Secure Tunnel credentials\n5. Run ./auth.sh login\n6. Run ./start.sh\n'

