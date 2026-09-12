#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
found=0
for name in manager tunnel mcp; do
  file="$RUN_DIR/$name.pid"
  [ -f "$file" ] || continue
  pid=$(sed -n '1p' "$file")
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    printf 'Stopping %s (PID %s)...\n' "$name" "$pid"
    kill -TERM "$pid"
    found=1
    for _ in {1..50}; do kill -0 "$pid" 2>/dev/null || break; sleep .1; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
  fi
  rm -f "$file"
done
rm -f "$RUN_DIR/tunnel-health.url" "$RUN_DIR/health.json"
rmdir "$RUN_DIR/manager.lock" 2>/dev/null || true
[ "$found" -eq 1 ] && printf 'Managed processes stopped.\n' || printf 'No managed processes are running.\n'

