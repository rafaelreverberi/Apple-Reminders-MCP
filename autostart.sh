#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME=apple-reminders-mcp.service
FILE="/etc/systemd/system/$NAME"
ACTION="${1:-status}"
[ "$(uname -s)" = Linux ] || { printf 'Error: systemd setup is Linux-only.\n' >&2; exit 1; }
command -v systemctl >/dev/null
command -v sudo >/dev/null
case "$ACTION" in
  setup)
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    sed -e "s|@@USER@@|$(id -un)|g" -e "s|@@GROUP@@|$(id -gn)|g" -e "s|@@HOME@@|$HOME|g" -e "s|@@ROOT@@|$ROOT_DIR|g" "$ROOT_DIR/systemd/apple-reminders-mcp.service.template" > "$tmp"
    sudo install -m 0644 "$tmp" "$FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$NAME"
    sudo systemctl --no-pager --full status "$NAME" || true
    ;;
  status) sudo systemctl --no-pager --full status "$NAME" || true ;;
  restart) sudo systemctl restart "$NAME"; sudo systemctl --no-pager --full status "$NAME" || true ;;
  logs) sudo journalctl -u "$NAME" -n 200 --no-pager ;;
  remove) sudo systemctl disable --now "$NAME" 2>/dev/null || true; sudo rm -f "$FILE"; sudo systemctl daemon-reload ;;
  *) printf 'Usage: ./autostart.sh setup|status|restart|logs|remove\n' >&2; exit 1 ;;
esac

