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

VPS aggregation (optional)
--------------------------
If a VPS is configured (base_url + token, see _load_vps_config), this server
also becomes a *proxy* for the :3333 dashboard's terminal API - which exposes
the byte-identical routes. Its sessions are merged into /sessions and /states
with their ids namespaced "vps:<id>", and any control call (stream, input,
resize, stop, rename, upload, start) whose sid carries that prefix is forwarded
to the box with a Bearer token. The token therefore stays on this machine and
never reaches the browser. When no VPS is configured, behaviour is unchanged.

Usage:
    python3 server.py [--port N] [--home DIR]
                      [--vps-url URL] [--vps-token TOKEN]
    python3 server.py --selftest      # round-trips a token through a real tmux
                                        # shell and prints VERIFY_OK / exits 1

Stdlib only. Requires tmux on PATH (terminal_manager talks to it).
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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


# ── VPS aggregation ────────────────────────────────────────────────────────
# When configured, VPS session ids are namespaced with this prefix so they can
# never collide with a local tmux id and so the router knows where to send a
# control call. The frontend treats the whole thing as an opaque id.
SID_VPS_PREFIX = "vps:"

# Bound once in main()/_run_selftest via _load_vps_config(). Empty base = the
# proxy is off and this behaves exactly like the pre-VPS local-only server.
VPS_BASE = ""     # e.g. "http://72.62.195.38:3333" (no trailing slash)
VPS_TOKEN = ""    # dashboard Bearer token
VPS_TIMEOUT = 6   # seconds for non-streaming calls; the box is one hop away


# The .app and run-terminal.sh keep their config in different places; we read
# whichever exists so neither launcher has to learn about the vps block.
_CONFIG_CANDIDATES = [
    os.path.join(HERE, "mts-config.json"),
    os.path.expanduser(
        "~/Library/Application Support/MLSuiteTerminal/mts-config.json"),
]


def _load_vps_config(cli_url=None, cli_token=None):
    """Resolve the VPS base url + token, most-specific source winning:
    CLI flags > env (MTS_VPS_URL / MTS_VPS_TOKEN) > a "vps" block in either
    mts-config.json. A base url with no token (or vice versa) leaves the proxy
    off - both are required. Sets the module globals and returns (base, token)."""
    global VPS_BASE, VPS_TOKEN, VPS_PATH_MAP

    base = cli_url or os.environ.get("MTS_VPS_URL", "")
    token = cli_token or os.environ.get("MTS_VPS_TOKEN", "")
    path_map = None

    # Read every candidate: the path map may live in a different file than the
    # credentials, so this cannot stop at the first one that has a base+token.
    for path in _CONFIG_CANDIDATES:
        try:
            with open(path) as f:
                vps = (json.load(f) or {}).get("vps") or {}
        except (OSError, ValueError):
            continue
        base = base or vps.get("base_url", "") or vps.get("url", "")
        token = token or vps.get("token", "")
        if path_map is None and isinstance(vps.get("path_map"), dict):
            path_map = vps["path_map"]

    VPS_BASE = (base or "").rstrip("/")
    VPS_TOKEN = token or ""
    VPS_PATH_MAP = path_map if path_map is not None else dict(_DEFAULT_PATH_MAP)
    return VPS_BASE, VPS_TOKEN


def _vps_enabled():
    return bool(VPS_BASE and VPS_TOKEN)


def _split_sid(sid):
    """('vps', real_id) for a namespaced sid, else ('local', sid)."""
    if sid and sid.startswith(SID_VPS_PREFIX):
        return "vps", sid[len(SID_VPS_PREFIX):]
    return "local", sid


def _vps_headers():
    return {"Authorization": "Bearer " + VPS_TOKEN}


def _vps_get(path):
    """GET json from the box. Returns the decoded dict, or None on any failure -
    the caller degrades to local-only rather than erroring the whole request."""
    try:
        req = urllib.request.Request(VPS_BASE + path, headers=_vps_headers())
        with urllib.request.urlopen(req, timeout=VPS_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _vps_post(path, body, timeout=None):
    """POST json to the box, return the decoded dict (or an error dict)."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            VPS_BASE + path, data=data, method="POST",
            headers={**_vps_headers(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or VPS_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "vps http %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": "vps unreachable: %s" % e}


def _vps_start(name=None):
    """Start a session on the box and namespace the id it returns, so the
    frontend switches straight to the new vps: pane."""
    res = _vps_post("/api/terminal/start", {"name": name})
    if res and res.get("ok") and res.get("session"):
        sess = res["session"]
        sess["realid"] = sess.get("id", "")
        sess["id"] = SID_VPS_PREFIX + sess.get("id", "")
        sess["host"] = "vps"
    return res


def _vps_open_stream(real_id):
    """Open the box's SSE stream for a session; returns the live response object
    (the caller pumps it) or None. A generous read timeout survives the stream's
    idle gaps between keepalive pings but still unblocks if the box dies."""
    try:
        url = VPS_BASE + "/api/terminal/stream?sid=" + urllib.request.quote(real_id)
        req = urllib.request.Request(url, headers=_vps_headers())
        return urllib.request.urlopen(req, timeout=120)
    except Exception:
        return None


def _merged_sessions():
    """Local sessions first (host=local), then the box's (host=vps, id namespaced).
    Always ok:true; a `vps` block tells the frontend whether the box is
    configured and currently reachable, so it can show the section as offline."""
    res = terminal_manager.list_sessions()
    out = []
    for s in res.get("sessions", []):
        s = dict(s)
        s["host"] = "local"
        out.append(s)

    vps_info = {"enabled": _vps_enabled(), "online": False,
                "host": urlparse(VPS_BASE).netloc if VPS_BASE else ""}
    if _vps_enabled():
        data = _vps_get("/api/terminal/sessions")
        if data and data.get("ok"):
            vps_info["online"] = True
            for s in data.get("sessions", []):
                s = dict(s)
                s["realid"] = s.get("id", "")
                s["id"] = SID_VPS_PREFIX + s.get("id", "")
                s["host"] = "vps"
                out.append(s)
    return {"ok": True, "sessions": out, "vps": vps_info}


def _merged_states():
    """Local states, plus the box's states re-keyed under the vps: prefix."""
    res = terminal_manager.all_states()
    states = dict(res.get("states", {}))
    if _vps_enabled():
        data = _vps_get("/api/terminal/states")
        if data and data.get("ok"):
            for sid, st in (data.get("states") or {}).items():
                states[SID_VPS_PREFIX + sid] = st
    res["states"] = states
    return res


# ── Send a local session to the box ────────────────────────────────────────
# A running process cannot be moved between machines - its memory, fds and tty
# are Mac-local - so "send to VPS" is a *handoff*, not a migration: the local
# Claude writes a brief of what it is doing, and a fresh session on the box is
# started with that brief as its opening task. The local session is left alone.

# Local path prefix -> box path prefix; longest match wins. Override with a
# "path_map" key in the vps config block when another repo gets a box clone.
VPS_PATH_MAP = {}
_DEFAULT_PATH_MAP = {"~/Documents/AI-Hub": "/home/aihub/AI-Hub"}

# The brief goes to a temp path, never into the repo - a file in the working
# tree would show up in git status and could be committed by accident.
BRIEF_DIR = "/tmp"
BRIEF_TIMEOUT = 300     # the session may be mid-task; the typed prompt queues
BRIEF_POLL = 2
_HEREDOC_EOF = "MTS_HANDOFF_EOF"


def _map_path_to_vps(local_path):
    """Translate a Mac path to its counterpart on the box, or None if the
    directory has no known equivalent there (better to refuse than to drop the
    session into an unrelated folder)."""
    if not local_path:
        return None
    best_local, best_remote = "", None
    for loc, rem in VPS_PATH_MAP.items():
        loc = os.path.expanduser(loc).rstrip("/")
        if (local_path == loc or local_path.startswith(loc + "/")) \
                and len(loc) > len(best_local):
            best_local, best_remote = loc, rem.rstrip("/")
    if best_remote is None:
        return None
    return best_remote + local_path[len(best_local):]


def _git_dirty(cwd):
    """Uncommitted paths in cwd's repo, or [] when clean / not a repo. The box
    works from its own clone, so a dirty tree means the two would disagree."""
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [ln for ln in (r.stdout or "").splitlines() if ln.strip()]


def _brief_path(sid):
    return os.path.join(BRIEF_DIR, "mts-handoff-%s.md" % sid)


# Claude Code treats a burst of input as a paste, and a carriage return inside
# a paste is a line break, not "submit" - send the text and the Enter together
# and the prompt just sits in the input box forever. So the return goes as its
# own keystroke, after a beat. (Deliberately no Ctrl-C to clear a stray draft
# first: on an idle Claude a double Ctrl-C quits it, which would destroy the
# very session we are trying to hand over.)
_SUBMIT_DELAY = 0.9


def _request_brief(sid, path, vps_cwd):
    """Type the brief request into the local session, then submit it."""
    prompt = (
        "Write a handoff brief to %s so another Claude on our VPS can continue "
        "this exact work: what we are building, what is done, what is next, the "
        "key files with their paths, and any decisions or gotchas it must know. "
        "That machine has the repo at %s instead of the local path. Do not ask "
        "me anything first - if this session has little context, say so in the "
        "file instead of asking. Write the file, then reply DONE."
        % (path, vps_cwd)
    )
    res = terminal_manager.write_session(sid, prompt)
    if not (res and res.get("ok")):
        return res
    time.sleep(_SUBMIT_DELAY)
    return terminal_manager.write_session(sid, "\r")


def _await_brief(path, timeout=BRIEF_TIMEOUT):
    """Wait for the brief to appear and stop growing, so a half-written file is
    never shipped. Returns its text, or None on timeout."""
    deadline = time.time() + timeout
    last_size, stable = -1, 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(BRIEF_POLL)
            continue
        stable = stable + 1 if size == last_size and size > 0 else 0
        last_size = size
        if stable >= 2:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read().strip()
                return text or None
            except OSError:
                return None
        time.sleep(BRIEF_POLL)
    return None


def _seed_vps_session(vps_real_id, vps_cwd, brief):
    """Plant the brief on the box and open Claude on it.

    The brief is delivered through a quoted heredoc, so the shell performs no
    expansion at all and arbitrary prose - backticks, $, quotes - travels
    intact. A line equal to the delimiter would end it early, so those are
    defanged first."""
    safe = "\n".join(
        ("  " + ln) if ln.strip() == _HEREDOC_EOF else ln
        for ln in brief.splitlines()
    )
    remote = "/tmp/mts-handoff-%s.md" % vps_real_id
    script = (
        "cd %s && cat > %s <<'%s'\n%s\n%s\n"
        % (vps_cwd, remote, _HEREDOC_EOF, safe, _HEREDOC_EOF)
    )
    res = _vps_post("/api/terminal/input", {"sid": vps_real_id, "data": script},
                    timeout=30)
    if not (res and res.get("ok")):
        return {"ok": False, "error": "could not write the brief on the box"}
    # Separate call: the heredoc must be closed before the next command is fed.
    # Read the brief into a variable and delete the file before Claude starts,
    # so a handoff never leaves project context lying around in the box's /tmp.
    time.sleep(0.6)
    return _vps_post(
        "/api/terminal/input",
        {"sid": vps_real_id,
         "data": 'B="$(cat %s)"; rm -f %s; claude "$B"\r' % (remote, remote)},
        timeout=30)


def send_local_to_vps(sid, confirm=False, name=None):
    """Hand a local session's work over to a new session on the box."""
    if not _vps_enabled():
        return {"ok": False, "error": "vps not configured"}

    sessions = {s["id"]: s for s in
                terminal_manager.list_sessions().get("sessions", [])}
    sess = sessions.get(sid)
    if not sess:
        return {"ok": False, "error": "no such local session"}

    cwd = terminal_manager.session_cwd(sid)
    vps_cwd = _map_path_to_vps(cwd)
    if not vps_cwd:
        return {"ok": False, "error":
                "no VPS path is mapped for %s - add one under vps.path_map"
                % (cwd or "this session")}

    dirty = _git_dirty(cwd)
    if dirty and not confirm:
        return {"ok": False, "needs_confirm": True, "cwd": cwd,
                "vps_cwd": vps_cwd, "dirty": dirty[:40],
                "dirty_total": len(dirty)}

    # A plain shell has no conversation to hand over; only ask for a brief when
    # Claude is actually running in the pane. Claude Code renames its process to
    # its version number, so tmux reports the foreground command as e.g.
    # "2.1.199" rather than "claude" - terminal_manager already knows that shape,
    # so reuse its test instead of matching on the name.
    cmd = (sess.get("cmd") or "").strip().lower()
    running_claude = (cmd in terminal_manager._AMBIGUOUS_RUNTIME
                      or terminal_manager._looks_like_version(cmd))
    brief = None
    if running_claude:
        typed = _request_brief(sid, _brief_path(sid), vps_cwd)
        if not (typed and typed.get("ok")):
            return {"ok": False, "error": "could not prompt the local session"}
        brief = _await_brief(_brief_path(sid))
        if not brief:
            return {"ok": False, "error":
                    "the local session did not produce a brief within %ds - it "
                    "may be mid-task or waiting on a permission prompt"
                    % BRIEF_TIMEOUT}

    started = _vps_start(name or ("%s (from Mac)" % sess.get("name", "session")))
    if not (started and started.get("ok") and started.get("session")):
        return {"ok": False, "error": "could not start a session on the box"}
    new = started["session"]
    real = new.get("realid") or ""

    if brief:
        seeded = _seed_vps_session(real, vps_cwd, brief)
        if not (seeded and seeded.get("ok")):
            # The session exists and is usable, so report it rather than
            # stranding the user with a pane they cannot find.
            return {"ok": True, "session": new, "vps_cwd": vps_cwd,
                    "warning": "session started, but the brief did not land - "
                               "it is at %s on this Mac" % _brief_path(sid)}
        try:
            os.remove(_brief_path(sid))
        except OSError:
            pass
    else:
        _vps_post("/api/terminal/input",
                  {"sid": real, "data": "cd %s\r" % vps_cwd}, timeout=15)

    return {"ok": True, "session": new, "vps_cwd": vps_cwd,
            "briefed": bool(brief)}


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
            self._json(200, _merged_sessions())
            return
        if path == "/api/terminal/states":
            self._json(200, _merged_states())
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

        # A vps: sid streams from the box; we just relay its bytes.
        host, real = _split_sid(sid)
        if host == "vps":
            self._stream_vps(real, sid)
            return

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

    def _stream_vps(self, real_id, full_sid):
        """Relay the box's SSE stream for a vps: session straight through. The
        box already emits the same data: lines the frontend parses (output,
        replay, exit, ping); we forward them line by line so nothing buffers.
        The upstream's own `session` event carries the box-local id, which the
        frontend ignores, so no rewriting is needed."""
        if not _vps_enabled():
            self._json(404, {"error": "vps not configured"})
            return
        upstream = _vps_open_stream(real_id)
        if upstream is None:
            self._json(502, {"ok": False, "error": "vps stream unreachable"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            # Announce our namespaced id first (matches the local path's shape).
            self.wfile.write(
                f"data: {json.dumps({'type': 'session', 'sid': full_sid})}\n\n".encode())
            self.wfile.flush()
            for raw in upstream:  # HTTPResponse yields one SSE line at a time
                self.wfile.write(raw)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser tab closed / switched away
        except Exception:
            pass  # box dropped the stream; the frontend reconnects on focus
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    # ── POST ──

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        data = self._read_json_body()
        if data is None:
            return  # _read_json_body already sent a 400

        # /start has no sid yet, so it routes on an explicit host field; every
        # other control call routes on its sid's vps: prefix.
        if path == "/api/terminal/start":
            if data.get("host") == "vps":
                self._json(200, _vps_start(data.get("name")))
                return
            self._json(200, terminal_manager.start_session(data.get("name")))
            return

        # Handing a local session over to the box is neither a local nor a
        # proxied call - it drives both - so it is routed before the sid split.
        # Slow by nature (it waits on the local Claude); ThreadingHTTPServer
        # keeps that off the other requests.
        if path == "/api/terminal/send-to-vps":
            sid = data.get("sid", "")
            if _split_sid(sid)[0] == "vps":
                self._json(400, {"ok": False,
                                 "error": "that session is already on the box"})
                return
            self._json(200, send_local_to_vps(
                sid, confirm=bool(data.get("confirm")), name=data.get("name")))
            return

        host, real = _split_sid(data.get("sid", ""))
        if host == "vps":
            if not _vps_enabled():
                self._json(404, {"ok": False, "error": "vps not configured"})
                return
            body = dict(data)
            body["sid"] = real          # the box knows its own un-namespaced id
            body.pop("host", None)
            # uploads carry up to 5MB of base64 - give the hop more time.
            tmo = 30 if path == "/api/terminal/upload" else None
            self._json(200, _vps_post(path, body, timeout=tmo))
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
        if path == "/api/terminal/upload":
            self._json(200, terminal_manager.save_upload(
                data.get("sid", ""), data.get("name", ""),
                data.get("mime", ""), data.get("b64", "")))
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
    parser.add_argument("--vps-url", default=None,
                        help="base url of the :3333 dashboard to aggregate "
                             "(e.g. http://72.62.195.38:3333)")
    parser.add_argument("--vps-token", default=None,
                        help="dashboard Bearer token for --vps-url")
    parser.add_argument("--selftest", action="store_true",
                        help="round-trip a token through a real tmux shell, then exit")
    args = parser.parse_args()

    # Set TERMINAL_HOME BEFORE importing terminal_manager: it reads the env var
    # at import time to fix where new sessions are created.
    if args.home:
        os.environ["TERMINAL_HOME"] = os.path.abspath(os.path.expanduser(args.home))

    if args.selftest:
        sys.exit(_run_selftest())

    # Resolve the optional VPS proxy (flags > env > mts-config.json vps block).
    base, _tok = _load_vps_config(args.vps_url, args.vps_token)
    if _vps_enabled():
        print(f"Aggregating VPS sessions from {base}")

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
