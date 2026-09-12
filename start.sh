#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
LOG_DIR="$ROOT_DIR/logs"
RUN_DIR="$ROOT_DIR/.run"
PROFILE_DIR="$ROOT_DIR/state/tunnel-profiles"
PIDS=()
CLEANED=0
OWNS_LOCK=0
fail(){ printf 'Error: %s\n' "$*" >&2; exit 1; }
[ -f "$ENV_FILE" ] || fail "Missing .env; run ./setup.sh"
perm=$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")
[ "$perm" = 600 ] || fail ".env permissions must be 600 (currently $perm)"
set -a
. "$ENV_FILE"
set +a
: "${MCP_HOST:=127.0.0.1}"
: "${MCP_PORT:=13003}"
: "${MCP_PATH:=/mcp}"
: "${TUNNEL_PROFILE:=apple-reminders-mcp}"
: "${TUNNEL_HEALTH_LISTEN_ADDR:=127.0.0.1:0}"
case "$MCP_HOST" in 127.0.0.1|localhost|::1) ;; *) fail "MCP_HOST must be loopback" ;; esac
[ -n "${ICLOUD_USERNAME:-}" ] || fail "ICLOUD_USERNAME is empty"
[ -n "${CONTROL_PLANE_API_KEY:-}" ] || fail "CONTROL_PLANE_API_KEY is empty"
[ -n "${CONTROL_PLANE_TUNNEL_ID:-}" ] || fail "CONTROL_PLANE_TUNNEL_ID is empty"
[ "${#CONFIRMATION_SIGNING_SECRET}" -ge 32 ] || fail "CONFIRMATION_SIGNING_SECRET must be at least 32 characters"
command -v curl >/dev/null || fail "curl not found"
if [ -n "${TUNNEL_CLIENT_BIN:-}" ]; then TUNNEL_BIN="$TUNNEL_CLIENT_BIN"
elif command -v tunnel-client >/dev/null; then TUNNEL_BIN="$(command -v tunnel-client)"
elif [ -x "$HOME/.local/bin/tunnel-client" ]; then TUNNEL_BIN="$HOME/.local/bin/tunnel-client"
else fail "tunnel-client not found"; fi
[ -x "$TUNNEL_BIN" ] || fail "tunnel-client is not executable"
if [ -z "${SSL_CERT_FILE:-}" ]; then
  SSL_CERT_FILE=$("$ROOT_DIR/apple-reminders-mcp-server/.venv/bin/python" -c 'import certifi; print(certifi.where())')
  export SSL_CERT_FILE
fi
: "${REQUESTS_CA_BUNDLE:=$SSL_CERT_FILE}"
export REQUESTS_CA_BUNDLE
mkdir -p "$LOG_DIR" "$RUN_DIR" "$PROFILE_DIR"
chmod 700 "$RUN_DIR" "$PROFILE_DIR"
if ! mkdir "$RUN_DIR/manager.lock" 2>/dev/null; then
  pid=$(sed -n '1p' "$RUN_DIR/manager.pid" 2>/dev/null || true)
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && fail "already running (manager PID $pid)"
  rmdir "$RUN_DIR/manager.lock" 2>/dev/null || true
  mkdir "$RUN_DIR/manager.lock"
fi
OWNS_LOCK=1
printf '%s\n' "$$" > "$RUN_DIR/manager.pid"
cleanup(){
  [ "$OWNS_LOCK" -eq 1 ] || return
  [ "$CLEANED" -eq 0 ] || return
  CLEANED=1
  trap - INT TERM EXIT
  for pid in "${PIDS[@]:-}"; do kill -TERM "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]:-}"; do wait "$pid" 2>/dev/null || true; done
  rm -f "$RUN_DIR"/*.pid "$RUN_DIR/tunnel-health.url" "$RUN_DIR/health.json"
  rmdir "$RUN_DIR/manager.lock" 2>/dev/null || true
}
trap cleanup INT TERM EXIT
if command -v lsof >/dev/null 2>&1 && lsof -nP -tiTCP:"$MCP_PORT" -sTCP:LISTEN | grep -q .; then fail "MCP port $MCP_PORT is already in use"; fi
MCP_URL="http://$MCP_HOST:$MCP_PORT$MCP_PATH"
HEALTH_URL="http://$MCP_HOST:$MCP_PORT/health"
printf '[1/2] Starting Apple Reminders MCP...\n'
(cd "$ROOT_DIR/apple-reminders-mcp-server" && exec .venv/bin/python -m apple_reminders_mcp) >>"$LOG_DIR/apple-reminders-mcp.log" 2>&1 &
MCP_PID=$!
PIDS+=("$MCP_PID")
printf '%s\n' "$MCP_PID" > "$RUN_DIR/mcp.pid"
for _ in {1..30}; do
  curl -fsS --max-time 2 "$HEALTH_URL" > "$RUN_DIR/health.json" 2>/dev/null && break
  kill -0 "$MCP_PID" 2>/dev/null || fail "MCP exited; inspect logs/apple-reminders-mcp.log"
  sleep 1
done
[ -s "$RUN_DIR/health.json" ] || fail "MCP health endpoint did not become ready"
SESSION="trusted"
grep -q 'reauthentication_required' "$RUN_DIR/health.json" && SESSION="reauthentication required"
printf '[2/2] Configuring OpenAI Secure MCP Tunnel...\n'
"$TUNNEL_BIN" init --force --sample sample_mcp_remote_no_auth --profile "$TUNNEL_PROFILE" --profile-dir "$PROFILE_DIR" --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" --mcp-server-url "$MCP_URL" --health-listen-addr "$TUNNEL_HEALTH_LISTEN_ADDR" >/dev/null
"$TUNNEL_BIN" doctor --profile "$TUNNEL_PROFILE" --profile-dir "$PROFILE_DIR" --explain
"$TUNNEL_BIN" run --profile "$TUNNEL_PROFILE" --profile-dir "$PROFILE_DIR" --health.listen-addr "$TUNNEL_HEALTH_LISTEN_ADDR" --health.url-file "$RUN_DIR/tunnel-health.url" >>"$LOG_DIR/tunnel.log" 2>&1 &
TUNNEL_PID=$!
PIDS+=("$TUNNEL_PID")
printf '%s\n' "$TUNNEL_PID" > "$RUN_DIR/tunnel.pid"
sleep 2
kill -0 "$TUNNEL_PID" 2>/dev/null || fail "Secure Tunnel exited; inspect logs/tunnel.log"
printf '\nApple Reminders MCP is ready.\n\nMCP:              %s\niCloud session:   %s\nSecure Tunnel:    running\n' "$MCP_URL" "$SESSION"
[ "$SESSION" = trusted ] || printf '\nRun ./auth.sh login, then ./autostart.sh restart\n'
while :; do
  for pid in "$MCP_PID" "$TUNNEL_PID"; do kill -0 "$pid" 2>/dev/null || fail "A managed process exited; inspect logs"; done
  sleep 2
done
