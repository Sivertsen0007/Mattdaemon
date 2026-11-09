#!/usr/bin/env bash
# run-terminal.sh - launch the local Mac terminal WITHOUT building the native
# .app. It starts the bundled Python engine (server.py) in the background, waits
# for it to come up, then opens it in your default browser. This is the quick
# path: no Xcode, no swift build, no codesigning - just the same terminal the
# .app wraps, served on loopback.
#
# The working directory is not set here. On a first run the app asks for it - and
# for a Claude login - on screen, and stores both per user (see setup.py). This
# script only needs to know which port to open.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PORT=8722

# Ask setup.py for the port and the configured folder, so this script and the
# server can never disagree about either. A blank folder means setup has not run
# yet, which is a thing to say out loud rather than a failure.
{
    read -r PORT
    read -r HOME_DIR
} < <(python3 - "$DIR" "$DEFAULT_PORT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import setup

cfg = setup.load_config()
try:
    port = int(cfg.get("port") or sys.argv[2])
except (TypeError, ValueError):
    port = int(sys.argv[2])
print(port)
print(setup.configured_home() if setup.is_complete() else "")
PY
)

URL="http://127.0.0.1:$PORT/"

if [[ -n "$HOME_DIR" ]]; then
    echo "==> Working directory: $HOME_DIR"
else
    echo "==> First run - the page will ask for a Claude login and a folder"
fi
echo "==> Starting terminal server on $URL"
python3 "$DIR/server.py" --port "$PORT" &
SERVER_PID=$!

# Stop the server we started when this script exits (Ctrl-C, error, or normal).
cleanup() {
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Wait for the server to answer before opening the browser (up to ~30s).
ready=0
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "$URL" 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "!! server exited before it came up" >&2
        exit 1
    fi
    sleep 0.5
done

if [[ "$ready" -ne 1 ]]; then
    echo "!! server did not respond at $URL in time" >&2
    exit 1
fi

echo "==> Opening $URL"
if command -v open >/dev/null 2>&1; then
    open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL"
else
    echo "   (could not find 'open' or 'xdg-open' - browse to $URL yourself)"
fi

echo "==> Terminal is running. Press Ctrl-C to stop the server."
wait "$SERVER_PID"
