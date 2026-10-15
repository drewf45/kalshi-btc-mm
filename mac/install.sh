#!/bin/bash
# WO-ROT-1 Chunk 6 — one-line install of the Rotation Desk on a Mac:
#
#   bash mac/install.sh            # install / update and start
#   bash mac/install.sh uninstall  # stop and remove the launch agent
#
# Safe to re-run. Never touches your .env once it exists.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Library/Application Support/Kal"
LOGS="$HOME/Library/Logs/Kal"
AGENT="$HOME/Library/LaunchAgents/com.kal.rotationdesk.plist"
LABEL="com.kal.rotationdesk"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$AGENT"
  echo "Removed $LABEL (your data in '$APP' is untouched)."
  exit 0
fi

[ "$(uname)" = "Darwin" ] || { echo "This installer is for macOS."; exit 1; }

mkdir -p "$APP" "$LOGS" "$HOME/Library/LaunchAgents"

# 1. Python environment (a venv inside the repo, never committed)
PY3="$(command -v python3 || true)"
[ -n "$PY3" ] || { echo "python3 not found — install it (e.g. from python.org) and re-run."; exit 1; }
[ -d "$REPO/.venv" ] || "$PY3" -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install --quiet --upgrade pip
"$REPO/.venv/bin/pip" install --quiet -r "$REPO/requirements.txt"

# 2. Secrets file: created once from the example, mode 600
if [ ! -f "$APP/.env" ]; then
  cp "$REPO/mac/env.example" "$APP/.env"
  chmod 600 "$APP/.env"
  echo "Created '$APP/.env' (mode 600) — fill in your keys, then re-run this script."
  exit 0
fi
chmod 600 "$APP/.env"

# 3. The launch agent (KeepAlive + RunAtLoad, wrapped in caffeinate -i -s)
sed -e "s#__REPO__#$REPO#g" -e "s#__PYTHON__#$REPO/.venv/bin/python#g" \
    -e "s#__HOME__#$HOME#g" "$REPO/mac/com.kal.rotationdesk.plist" > "$AGENT"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$AGENT"
launchctl enable "$DOMAIN/$LABEL"

echo "Started $LABEL."
echo "  logs:      $LOGS/kal.log"
echo "  heartbeat: $APP/heartbeat.json  (check: $REPO/.venv/bin/python -m relay_engine.deadman)"
echo "  database:  $APP/kal.db"
echo "Keep the Mac on AC power with the lid open — on battery the desk stops new entries."
