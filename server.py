#!/usr/bin/env python3
"""
Standalone terminal server for the local macOS terminal app.

A tiny http.server that exposes ONLY the terminal - it replaces the AI-Hub
dashboard server (shared/server.py) for this app. The request/response shapes
mirror the dashboard's terminal routes exactly, so the same frontend talks to
either one unchanged. The tmux-backed session logic lives in the sibling
terminal_manager.py, which we import.

Local app => we bind to 127.0.0.1 ONLY, never 0.0.0.0. There is no auth here;
loopback is the boundary.

Usage:
    python3 server.py [--port N] [--home DIR]
    python3 server.py --selftest      # round-trips a token through a real tmux
                                        # shell and prints VERIFY_OK / exits 1

Stdlib only. Requires tmux on PATH (terminal_manager talks to it).
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# terminal_manager reads TERMINAL_HOME at import time to decide where new
# sessions start (its module-level START_DIR). So --home MUST be applied to the
# environment before that import happens - see main(). We therefore do NOT
# import terminal_manager at module top; it is imported after arg parsing.
terminal_manager = None  # bound in main()/_run_selftest once TERMINAL_HOME is set

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# Content types for the handful of things we serve out of static/.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}


def _ctype_for(path):
    return _CONTENT_TYPES.get(os.path.splitext(path)[1].lower(),
                              "application/octet-stream")


class TerminalHandler(BaseHTTPRequestHandler):
    # Quiet by default; the request log is noise for a local app.
    def log_message(self, fmt, *args):
        pass

    # ── response helpers ──

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath, content_type):
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except (FileNotFoundError, IsADirectoryError):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return None

    # ── GET ──

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/":
            self._serve_file(os.path.join(STATIC_DIR, "index.html"),
                             "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/api/terminal/stream":
            self._stream_terminal()
            return
        if path == "/api/terminal/sessions":
            self._json(200, terminal_manager.list_sessions())
            return
        if path == "/api/terminal/states":
            self._json(200, terminal_manager.all_states())
            return

        self._json(404, {"error": "not found"})

    def _serve_static(self, rel):
        # Path-traversal-safe: the resolved file must stay under static/.
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        root = os.path.realpath(STATIC_DIR)
        if not (target == root or target.startswith(root + os.sep)):
            self._json(404, {"error": "not found"})
            return
        if not os.path.isfile(target):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self._serve_file(target, _ctype_for(target))

    def _stream_terminal(self):
        """Stream a session's output as Server-Sent Events.

        Mirrors the dashboard: if no sid is given, a fresh session is started
        and its id is announced as the first event so the client knows what it
        got. X-Accel-Buffering: no keeps any reverse proxy from buffering the
        stream (harmless when there is none)."""
        qs = parse_qs(urlparse(self.path).query)
        sid = qs.get("sid", [None])[0]

        if not sid:
            result = terminal_manager.start_session()
            if not result.get("ok"):
                self._json(500, result)
                return
            sid = result["session"]["id"]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            self.wfile.write(
                f"data: {json.dumps({'type': 'session', 'sid': sid})}\n\n".encode())
            self.wfile.flush()
            for chunk in terminal_manager.stream_session_output(sid):
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; the tmux session stays alive

    # ── POST ──

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        data = self._read_json_body()
        if data is None:
            return  # _read_json_body already sent a 400

        if path == "/api/terminal/start":
            self._json(200, terminal_manager.start_session(data.get("name")))
            return
        if path == "/api/terminal/input":
            self._json(200, terminal_manager.write_session(
                data.get("sid", ""), data.get("data", "")))
            return
        if path == "/api/terminal/resize":
            self._json(200, terminal_manager.resize_session(
                data.get("sid", ""), data.get("cols", 80), data.get("rows", 24)))
            return
        if path == "/api/terminal/stop":
            self._json(200, terminal_manager.stop_session(data.get("sid", "")))
            return
        if path == "/api/terminal/rename":
            self._json(200, terminal_manager.rename_session(
                data.get("sid", ""), data.get("name", "")))
            return

        self._json(404, {"error": "not found"})


def _make_server(port):
    """A loopback-only threaded HTTP server. Threaded so an open SSE stream
    never blocks other requests (input/resize/stop must land while streaming)."""
    return ThreadingHTTPServer(("127.0.0.1", port), TerminalHandler)


def _import_manager():
    """Import terminal_manager from this directory, after TERMINAL_HOME is set."""
    global terminal_manager
    if terminal_manager is not None:
        return terminal_manager
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import terminal_manager as tm
    terminal_manager = tm
    return tm


def _run_selftest():
    """Prove the whole path works against a real tmux shell.

    Boots the actual HTTP server on an ephemeral loopback port, then drives it
    exactly as the browser would: start a session, open the SSE stream, type
    `echo farm-ok-token`, and assert the token comes back base64-decoded from
    the stream (i.e. out of the real shell). Prints VERIFY_OK / exits 0 on
    success, exits 1 on any failure or timeout."""
    import base64
    import time
    import urllib.request

    tm = _import_manager()
    if not tm._tmux_available():
        print("selftest FAIL: tmux not available", file=sys.stderr)
        return 1

    httpd = _make_server(0)  # bind 127.0.0.1:0 -> kernel picks a free port
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    TOKEN = "farm-ok-token"
    sid = None
    found = threading.Event()

    def _post(pth, obj):
        req = urllib.request.Request(
            base + pth, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _reader():
        # Read the SSE stream, base64-decode output/replay events, watch for the
        # token. Runs until the token appears or the connection is closed.
        try:
            with urllib.request.urlopen(base + f"/api/terminal/stream?sid={sid}",
                                        timeout=20) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") in ("output", "replay") and ev.get("data"):
                        try:
                            decoded = base64.b64decode(ev["data"]).decode(
                                "utf-8", "replace")
                        except (ValueError, TypeError):
                            continue
                        if TOKEN in decoded:
                            found.set()
                            return
        except Exception:
            pass  # stream closed / errored; found stays unset -> failure below

    ok = False
    try:
        started = _post("/api/terminal/start", {"name": "selftest"})
        if not started.get("ok"):
            print(f"selftest FAIL: start_session: {started}", file=sys.stderr)
            return 1
        sid = started["session"]["id"]

        rt = threading.Thread(target=_reader, daemon=True)
        rt.start()
        time.sleep(1.0)  # let the stream attach and the shell settle

        typed = _post("/api/terminal/input", {"sid": sid, "data": "echo %s\n" % TOKEN})
        if not typed.get("ok"):
            print(f"selftest FAIL: write_session: {typed}", file=sys.stderr)
            return 1

        ok = found.wait(timeout=15)
        if not ok:
            print("selftest FAIL: token never round-tripped from the shell",
                  file=sys.stderr)
    finally:
        if sid:
            try:
                _post("/api/terminal/stop", {"sid": sid})
            except Exception:
                pass
        httpd.shutdown()

    if ok:
        print("VERIFY_OK")
        return 0
    return 1


def main():
    parser = argparse.ArgumentParser(description="Standalone local terminal server")
    parser.add_argument("--port", type=int, default=8722)
    parser.add_argument("--home", default=None,
                        help="directory new terminal sessions start in")
    parser.add_argument("--selftest", action="store_true",
                        help="round-trip a token through a real tmux shell, then exit")
    args = parser.parse_args()

    # Set TERMINAL_HOME BEFORE importing terminal_manager: it reads the env var
    # at import time to fix where new sessions are created.
    if args.home:
        os.environ["TERMINAL_HOME"] = os.path.abspath(os.path.expanduser(args.home))

    if args.selftest:
        sys.exit(_run_selftest())

    _import_manager()

    httpd = _make_server(args.port)
    print(f"Terminal server on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        terminal_manager.stop_all()
        httpd.server_close()


if __name__ == "__main__":
    main()
