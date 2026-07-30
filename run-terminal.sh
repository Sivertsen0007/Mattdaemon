#!/usr/bin/env bash
# run-terminal.sh - launch the local Mac terminal WITHOUT building the native
# .app. It starts the bundled Python engine (server.py) in the background, waits
# for it to come up, then opens it in your default browser. This is the quick
# path: no Xcode, no swift build, no codesigning - just the same terminal the
# .app wraps, served on loopback.
#
# The working directory that new terminal sessions start in is read from
# mts-config.json (key "home"). If that file is missing it is created with a
# sane default. Change it there, not here.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/mts-config.json"
DEFAULT_HOME="$HOME/Documents/AI-Hub"
DEFAULT_PORT=8722

# Read home + port from mts-config.json, creating it with defaults if absent.
# We lean on python3 (already required by server.py) so we never need jq, and so
# a "~" in the config gets expanded to an absolute path.
{
    read -r HOME_DIR
    read -r PORT
} < <(python3 - "$CONFIG" "$DEFAULT_HOME" "$DEFAULT_PORT" <<'PY'
import json, os, sys

cfg, default_home, default_port = sys.argv[1], sys.argv[2], int(sys.argv[3])

data = {}
if os.path.exists(cfg):
    try:
        with open(cfg) as f:
            data = json.load(f)
    except Exception:
        data = {}

home = data.get("home", default_home)
port = data.get("port", default_port)

# Write a fresh config the first time, so the user has something to edit.
if not os.path.exists(cfg):
    with open(cfg, "w") as f:
        json.dump({"home": default_home, "port": default_port}, f, indent=2)
        f.write("\n")

print(os.path.abspath(os.path.expanduser(home)))
print(int(port))
PY
)

URL="http://127.0.0.1:$PORT/"

echo "==> Working directory: $HOME_DIR"
echo "==> Starting terminal server on $URL"
python3 "$DIR/server.py" --port "$PORT" --home "$HOME_DIR" &
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
