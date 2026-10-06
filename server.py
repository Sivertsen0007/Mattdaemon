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
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Name ourselves on every outbound request. The box's public door is the
# Cloudflare tunnel (https://hub.mlmediahub.com) since 2026-09-17, when :3333 was
# bound to localhost; Cloudflare's browser-integrity check answers urllib's default
# "Python-urllib/3.x" with 403 "error code: 1010" before the request ever reaches
# the box, so the app read the VPS as offline while curl, with its own agent, got
# straight through. Installed on the global opener so every urlopen here - JSON
# calls, the SSE stream, uploads - carries it, including ones added later.
_OPENER = urllib.request.build_opener()
_OPENER.addheaders = [("User-Agent", "Mattdaemon/1.0")]
urllib.request.install_opener(_OPENER)


# terminal_manager reads TERMINAL_HOME at import time to decide where new
# sessions start (its module-level START_DIR). So --home MUST be applied to the
# environment before that import happens - see main(). We therefore do NOT
# import terminal_manager at module top; it is imported after arg parsing.
terminal_manager = None  # bound in main()/_run_selftest once TERMINAL_HOME is set

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# First-run setup + the per-user config file. Safe to import at module level:
# setup.py touches neither TERMINAL_HOME nor terminal_manager on import.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import setup  # noqa: E402  (must follow the sys.path line above)

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
    # What term-show hands over, now that it opens in a browser: a type the
    # browser knows is the difference between showing the thing and downloading
    # it. A PDF report is the common one.
    ".pdf": "application/pdf",
    ".md": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".avif": "image/avif",
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
VPS_BASE = ""     # e.g. "http://192.0.2.10:3333" (no trailing slash)
VPS_TOKEN = ""    # dashboard Bearer token
VPS_TIMEOUT = 6   # seconds for non-streaming calls; the box is one hop away


# Where the config lives, how it merges and what may write it is setup.py's
# business - including the per-user file the settings panel writes at runtime.
# The CLI flags and env vars still win over all of it.
_cli_vps = {"url": None, "token": None}


def _load_vps_config(cli_url=None, cli_token=None):
    """Resolve the VPS base url + token, most-specific source winning:
    CLI flags > env (MTS_VPS_URL / MTS_VPS_TOKEN) > the "vps" block in the
    config (see setup.load_config). A base url with no token (or vice versa)
    leaves the proxy off - both are required. Sets the module globals and
    returns (base, token).

    Called again whenever the settings panel saves, so a VPS can be connected or
    disconnected without restarting the app.
    """
    global VPS_BASE, VPS_TOKEN, VPS_PATH_MAP

    if cli_url is not None:
        _cli_vps["url"] = cli_url
    if cli_token is not None:
        _cli_vps["token"] = cli_token

    vps = setup.load_config().get("vps")
    vps = vps if isinstance(vps, dict) else {}

    base = (_cli_vps["url"] or os.environ.get("MTS_VPS_URL", "")
            or vps.get("base_url", "") or vps.get("url", ""))
    token = (_cli_vps["token"] or os.environ.get("MTS_VPS_TOKEN", "")
             or vps.get("token", ""))
    path_map = vps.get("path_map")

    VPS_BASE = (base or "").rstrip("/")
    VPS_TOKEN = token or ""
    VPS_PATH_MAP = dict(path_map) if isinstance(path_map, dict) else {}
    return VPS_BASE, VPS_TOKEN


def _vps_enabled():
    return bool(VPS_BASE and VPS_TOKEN)


def _split_sid(sid):
    """('vps', real_id) for a namespaced sid, else ('local', sid)."""
    if sid and sid.startswith(SID_VPS_PREFIX):
        return "vps", sid[len(SID_VPS_PREFIX):]
    return "local", sid


def _vps_headers():
    # The User-Agent is not decoration. The box is reached through a Cloudflare
    # tunnel, and the tunnel answers 403 to urllib's default
    # "Python-urllib/3.x" - every call, sessions and plans alike, with a token
    # that is perfectly good. The app then shows the box as simply offline,
    # which is the one explanation that is not true. Any ordinary product token
    # passes, so send one.
    return {"Authorization": "Bearer " + VPS_TOKEN,
            "User-Agent": "Mattdaemon/1.0 (macOS)"}


def _vps_get_bytes(path):
    """GET raw bytes from the box (not JSON). None on any failure."""
    try:
        req = urllib.request.Request(VPS_BASE + path, headers=_vps_headers())
        with urllib.request.urlopen(req, timeout=VPS_TIMEOUT) as r:
            return r.read()
    except Exception:
        return None


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


def _vps_post_full(path, body, timeout=None):
    """POST to the box and keep the BODY even on a non-2xx.

    _vps_post throws the body away on an HTTPError, which is fine for calls whose
    failures are all alike. It is not fine for a worktree removal: the box
    answers 409 with {"dirty": true}, and that flag is the whole reason the
    client knows to ask again with force. Losing it turns a refusal you can
    recover from into a dead end that just says "vps http 409".
    """
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            VPS_BASE + path, data=data, method="POST",
            headers={**_vps_headers(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or VPS_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {"ok": False, "error": "vps http %s" % e.code}
    except Exception as e:
        return 0, {"ok": False, "error": "vps unreachable: %s" % e}


def _vps_start(name=None, autostart=False, agent=None):
    """Start a session on the box and namespace the id it returns, so the
    frontend switches straight to the new vps: pane.

    `agent` goes over as-is: the box runs the same registry this side does, and
    it answers for a name it does not know rather than starting the wrong agent.
    """
    res = _vps_post("/api/terminal/start",
                    {"name": name, "autostart": autostart, "agent": agent})
    if res and res.get("ok") and res.get("session"):
        sess = res["session"]
        sess["realid"] = sess.get("id", "")
        sess["id"] = SID_VPS_PREFIX + sess.get("id", "")
        sess["host"] = "vps"
    return res


def _vps_open_stream(real_id):
    """Open the box's SSE stream for a session; returns the live response object
    (the caller pumps it) or None.

    The read timeout is the only thing that unblocks this socket when the box
    becomes unreachable without closing the connection - which is exactly what a
    Mac waking from sleep leaves behind, since the TCP connection it slept on is
    gone but nobody told either end. The box pings every 15s, so 45s is three
    missed pings: long enough never to fire on a healthy idle stream, short
    enough that the relay dies and the frontend reconnects promptly. It used to
    be 120s, which left a woken Mac staring at a frozen terminal for two
    minutes."""
    try:
        url = VPS_BASE + "/api/terminal/stream?sid=" + urllib.request.quote(real_id)
        req = urllib.request.Request(url, headers=_vps_headers())
        return urllib.request.urlopen(req, timeout=45)
    except Exception:
        return None


# How long a vps: pane keeps trying to reach the box before it gives up and says
# so. Three quarters of the box's own stream read timeout is not the unit here:
# what matters is surviving a restart (seconds) and a short network sulk without
# surviving a box that is simply gone.
_RELAY_PATIENCE = 90.0


def _session_event_source(sid):
    """One session's events as dicts, wherever the shell actually lives.

    A local session comes straight from terminal_manager. A vps: one is relayed
    from the box, whose stream is already events of this same shape - we only
    have to parse its SSE lines back into events so the merged stream can tag
    and interleave them. The box's own `session` event carries a box-local id
    that would be wrong here, so it is dropped: the caller knows which sid it
    asked for.
    """
    host, real = _split_sid(sid)
    if host != "vps":
        yield from terminal_manager.iter_session_events(sid)
        return

    if not _vps_enabled():
        yield {"type": "error", "error": "vps not configured"}
        return

    # The relay reconnects itself, the way the local path re-attaches a dead
    # view. Anything that ends the box's stream - it restarts (several times a
    # day, since that is how it is deployed), the Mac sleeps, the network blips -
    # used to end this generator, and with it the pane. In a wall of four that is
    # invisible: the other three keep the merged response open, and the browser
    # only ever reconnects when the whole response ends, so the dead pane sat
    # there showing the last thing it drew - most often tmux's own "[terminated]"
    # from the client the box killed on its way down. Reconnecting instead gets
    # the pane a fresh attach, and the box replays the screen on every attach, so
    # it repaints rather than resuming mid-sentence.
    #
    # The patience is measured from the last event that actually arrived, not
    # from the start: a box that is answering keeps its pane for as long as it
    # keeps answering, and one that has genuinely gone releases it.
    deadline = time.time() + _RELAY_PATIENCE
    delay = 0.5
    while True:
        upstream = _vps_open_stream(real)
        if upstream is not None:
            delay = 0.5
            try:
                for raw in upstream:  # HTTPResponse yields one SSE line at a time
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "session":
                        continue
                    yield event
                    if kind == "exit":
                        return   # the shell is gone; there is nothing to re-open
                    deadline = time.time() + _RELAY_PATIENCE
            except Exception:
                pass  # a dead relay is a dropped connection, not a dead app
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
        if time.time() >= deadline:
            yield {"type": "error", "error": "vps stream lost"}
            return
        time.sleep(delay)
        delay = min(delay * 2, 5.0)


# ── The box's sessions, kept off the request path ──────────────────────────
# Asking the box inside a /sessions or /states request makes every local answer
# wait on a network hop - up to VPS_TIMEOUT of it. The page polls states every
# few seconds, so a slow or unreachable box meant those polls piling up on top
# of each other, each holding one of the browser's ~6 connections per origin.
# Once they are all held, everything else the page wants to do - a keystroke, a
# resize, closing a session, uploading a file - simply queues behind them. That
# is an app that gets stickier the longer it runs and stops responding to
# buttons, while the sessions behind it are perfectly healthy.
#
# So a background thread asks the box on its own clock and leaves the answer
# here. Requests read this and return at local speed, always.
_vps_snap = {"ts": 0.0, "sessions": [], "states": {}, "plans": {}, "files": {}}
_vps_snap_lock = threading.Lock()
_VPS_REFRESH = 3.0     # how often the poller asks the box
_VPS_STALE = 25.0      # older than this and we stop calling the box online


def _vps_snapshot():
    """(online, sessions, states, plans, files) from the background poller."""
    with _vps_snap_lock:
        snap = dict(_vps_snap)
    online = bool(snap["ts"]) and (time.time() - snap["ts"]) < _VPS_STALE
    return online, snap["sessions"], snap["states"], snap["plans"], snap["files"]


def _vps_poll_once():
    sessions = _vps_get("/api/terminal/sessions")
    if not (sessions and sessions.get("ok")):
        return   # leave the last good snapshot alone; it ages out on its own
    states = _vps_get("/api/terminal/states") or {}
    rows = []
    for s in sessions.get("sessions", []):
        s = dict(s)
        s["realid"] = s.get("id", "")
        s["id"] = SID_VPS_PREFIX + s.get("id", "")
        s["host"] = "vps"
        rows.append(s)
    pre = lambda d: {SID_VPS_PREFIX + k: v for k, v in (d or {}).items()}
    with _vps_snap_lock:
        _vps_snap.update({
            "ts": time.time(), "sessions": rows,
            "states": pre(states.get("states")), "plans": pre(states.get("plans")),
            "files": pre(states.get("files")),
        })


def _vps_snap_add(rows):
    """Put sessions the box has just created into the snapshot at once.

    Every listing this app serves for the box is read out of the poller's
    snapshot, which is up to _VPS_REFRESH seconds old. So a session created
    through here was invisible for as much as three seconds AFTER the call that
    made it had already returned its id: no row in the sidebar, a pane titled
    with a raw sid, and a click that had in fact worked reading as a click that
    hung. The answer to the create is the fresher fact, so it goes straight in
    rather than waiting to be discovered.
    """
    fresh = [dict(r) for r in (rows or []) if r.get("id")]
    if not fresh:
        return
    with _vps_snap_lock:
        have = {s.get("id") for s in _vps_snap["sessions"]}
        _vps_snap["sessions"] = _vps_snap["sessions"] + [
            r for r in fresh if r.get("id") not in have]
        # `ts` is deliberately not touched: this is one row we happen to know,
        # not a poll, and moving it would tell a box that has gone quiet that it
        # is still answering.


def _vps_snap_drop(sids):
    """Take sessions the box has just closed out of the snapshot at once.

    The same blind window as _vps_snap_add, in reverse: a shell you closed
    stayed in the list, with a live-looking dot, until the next poll.
    """
    gone = {s for s in (sids or []) if s}
    if not gone:
        return
    with _vps_snap_lock:
        _vps_snap["sessions"] = [s for s in _vps_snap["sessions"]
                                 if s.get("id") not in gone]


def _start_vps_poller():
    def loop():
        while True:
            try:
                if _vps_enabled():
                    _vps_poll_once()
            except Exception:
                pass   # a poller that dies takes the VPS section with it
            time.sleep(_VPS_REFRESH)
    threading.Thread(target=loop, daemon=True, name="vps-poller").start()


# How long a worktree call to the box may take. Not VPS_TIMEOUT: these three
# wait on git against a whole checkout, not on a tmux round-trip.
_WT_TIMEOUTS = {
    "/api/terminal/worktree/create": 180,
    "/api/terminal/worktree/add-session": 60,
    "/api/terminal/worktree/remove": 120,
}


def _merged_repos():
    """What each machine can cut a worktree from.

    Two separate lists, never pooled: a path on the box means nothing here and
    the other way round, so mixing them would offer you a folder that does not
    exist on the machine you picked.
    """
    local = terminal_manager.list_repos()
    hosts = {"local": {"repos": local.get("repos", []),
                       "default": local.get("default", ""), "online": True}}

    online, _s, _st, _pl, _fi = _vps_snapshot()
    vps_online = bool(_vps_enabled() and online)
    hosts["vps"] = {"repos": [], "default": "", "online": vps_online}
    if vps_online:
        remote = _vps_get("/api/terminal/repos")
        if remote and remote.get("ok"):
            hosts["vps"]["repos"] = remote.get("repos", [])
            hosts["vps"]["default"] = remote.get("default", "")
        else:
            hosts["vps"]["online"] = False
            hosts["vps"]["unsupported"] = True
    # Cloning reaches the credential store of the machine the app runs on, so it
    # is offered for this Mac only. The box has no token of its own yet.
    return {"ok": True, "hosts": hosts, "can_clone": {"local": True, "vps": False}}


def _merged_worktrees(with_dirty=False):
    """Local worktree groups, then the box's - group ids and member sids namespaced.

    A group id becomes the argument of every later call (add a session, remove
    the worktree), so a local "email-fix" and a box "email-fix" must not share
    one: without the prefix, the + on one folder would open a shell in the other.
    Same `vps:` convention the session ids already use.

    Each host answers for itself in `hosts`. The two are rarely on the same
    branch - this Mac follows one checkout, the box another - so the form asks
    the machine it is about to create on rather than assuming one global answer.
    """
    local = terminal_manager.list_worktree_groups(with_dirty=with_dirty)
    groups = []
    for g in local.get("worktrees", []):
        g = dict(g)
        g["host"] = "local"
        groups.append(g)

    hosts = {"local": {
        "current_branch": local.get("current_branch", ""),
        "folder": local.get("folder", ""),
        "is_repo": local.get("is_repo", True),
        "max_sessions": local.get("max_sessions", 6),
        "online": True,
    }}

    online, _s, _st, _pl, _fi = _vps_snapshot()
    vps_online = bool(_vps_enabled() and online)
    hosts["vps"] = {"current_branch": "", "folder": "", "is_repo": True,
                    "max_sessions": 6, "online": vps_online}
    if vps_online:
        remote = _vps_get("/api/terminal/worktrees")
        if remote and remote.get("ok"):
            for g in remote.get("worktrees", []):
                g = dict(g)
                g["realid"] = g.get("id", "")
                g["id"] = SID_VPS_PREFIX + g.get("id", "")
                g["sessions"] = [SID_VPS_PREFIX + s for s in (g.get("sessions") or [])]
                g["host"] = "vps"
                groups.append(g)
            hosts["vps"].update({
                "current_branch": remote.get("current_branch", ""),
                "folder": remote.get("folder", ""),
                # An older box does not send is_repo. Assume it is a repo rather
                # than greying out the option on a box that works perfectly well.
                "is_repo": remote.get("is_repo", True),
                "max_sessions": remote.get("max_sessions", 6),
            })
        else:
            # Reachable but this endpoint is not there: an older box, still
            # serving sessions. Say so instead of offering a button that 404s.
            hosts["vps"]["online"] = False
            hosts["vps"]["unsupported"] = True

    return {
        "ok": True, "worktrees": groups, "hosts": hosts,
        "vps": {"enabled": _vps_enabled(), "online": vps_online,
                "host": urlparse(VPS_BASE).netloc if VPS_BASE else ""},
        # Kept flat as well, for a client that has not learned about hosts yet.
        "current_branch": hosts["local"]["current_branch"],
        "is_repo": hosts["local"]["is_repo"],
        "folder": hosts["local"]["folder"],
        "max_sessions": hosts["local"]["max_sessions"],
    }


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

    online, vps_sessions, _st, _pl, _fi = _vps_snapshot()
    vps_info = {"enabled": _vps_enabled(), "online": _vps_enabled() and online,
                "host": urlparse(VPS_BASE).netloc if VPS_BASE else ""}
    if vps_info["online"]:
        out.extend(dict(s) for s in vps_sessions)
    return {"ok": True, "sessions": out, "vps": vps_info}


def _merged_states():
    """Local states, plus the box's states re-keyed under the vps: prefix.

    `plans` and `files` ride along on the same poll: they carry each session's
    @webplan and @webfile stamps, which is how a plan written by /farm and a
    file shown with term-show get opened in the browser."""
    res = terminal_manager.all_states()
    states = dict(res.get("states", {}))
    plans = dict(res.get("plans", {}))
    files = dict(res.get("files", {}))
    online, _sess, vstates, vplans, vfiles = _vps_snapshot()
    if _vps_enabled() and online:
        states.update(vstates)
        plans.update(vplans)
        files.update(vfiles)
    res["states"] = states
    res["plans"] = plans
    res["files"] = files
    return res


# ── Send a local session to the box ────────────────────────────────────────
# A running process cannot be moved between machines - its memory, fds and tty
# are Mac-local - so "send to VPS" is a *handoff*, not a migration: the local
# Claude writes a brief of what it is doing, and a fresh session on the box is
# started with that brief as its opening task. The local session is left alone.

# Local path prefix -> box path prefix; longest match wins. Comes entirely from
# the "path_map" key in the vps config block - there is no built-in default,
# because one machine's folder layout is nobody else's.
VPS_PATH_MAP = {}

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


# ── Bring the box's work back to this Mac ──────────────────────────────────
# The mirror of send-to-vps, and the same kind of thing - a process still
# cannot move between machines - with one part that the outbound direction
# does not need: the files. Going out, the box already has its own clone and
# only wants to know what to do. Coming back, the Mac's clone is behind by
# whatever the box has been building, so the handover is worthless unless the
# work is committed, pushed, and actually pulled down here first. That is the
# order below, and the local session is not opened until the commit the box
# named is present on this Mac.
#
# Reading the brief back off the box goes through its own file endpoint, which
# serves paths under the folder its dashboard was started on. The brief is
# written into the repo's .git directory: inside that folder, and invisible to
# git status, so a handover never leaves a stray file in the working tree.

PULL_WAIT = 120         # how long to keep fetching for the box's commit
PULL_POLL = 4
_HEADER_KEYS = ("CWD", "BRANCH", "COMMIT", "PUSHED")


def _map_path_from_vps(remote_path):
    """Translate a box path to its counterpart on this Mac, or None when the
    folder has no known equivalent here. The exact inverse of
    _map_path_to_vps, longest match and all."""
    if not remote_path:
        return None
    best_remote, best_local = "", None
    for loc, rem in VPS_PATH_MAP.items():
        rem = rem.rstrip("/")
        if (remote_path == rem or remote_path.startswith(rem + "/")) \
                and len(rem) > len(best_remote):
            best_remote = rem
            best_local = os.path.expanduser(loc).rstrip("/")
    if best_local is None:
        return None
    return best_local + remote_path[len(best_remote):]


def _vps_session_place(real_id):
    """(cwd, repo) for a session on the box.

    Its sessions API does not carry a working directory, but its diff endpoint
    does - it has to resolve one to run git there - so that is where this comes
    from rather than from a command typed into the pane, which would be
    impossible anyway while Claude owns the prompt."""
    res = _vps_get("/api/terminal/diff?sid=" + urllib.request.quote(real_id))
    if not (res and res.get("ok")):
        return "", ""
    return (res.get("cwd") or "").rstrip("/"), (res.get("repo") or "").rstrip("/")


def _remote_brief_path(repo, cwd, real_id):
    """Where the box should write its brief. Inside .git when there is a repo -
    under the served folder, and git never looks at it."""
    base = "mts-handoff-%s.md" % real_id
    return "%s/.git/%s" % (repo, base) if repo else "%s/.%s" % (cwd, base)


def _request_remote_brief(real_id, remote_file):
    """Ask the box's Claude to push its work and write a handoff brief.

    The header is fixed and machine-read: this end needs the commit to wait
    for, and asking for it in a known shape is more reliable than parsing prose.
    """
    prompt = (
        "Hand this work over to a Claude on my Mac. Do it in this order.\n\n"
        "1. Get the work off this machine: commit anything uncommitted with a "
        "clear message and push the current branch to origin. If there is "
        "nothing to commit, push what is already there. If pushing is not "
        "possible - no remote, no permission, detached HEAD - carry on anyway "
        "and say so.\n\n"
        "2. Write a handoff brief to %s. It must START with exactly these four "
        "lines, then a blank line:\n\n"
        "CWD: <absolute path of the working directory>\n"
        "BRANCH: <current git branch, or - >\n"
        "COMMIT: <full sha now on origin, or - >\n"
        "PUSHED: <yes or no>\n\n"
        "3. Below that, the brief itself: what we are building, what is done, "
        "what is next, the key files, and any decisions or gotchas it must "
        "know. The Mac has its own clone of this repo at a different path, so "
        "write file paths relative to the repo root where you can.\n\n"
        "Do not ask me anything first - if this session has little context, say "
        "so in the brief instead of asking. Write the file, then reply DONE."
        % remote_file
    )
    res = _vps_post("/api/terminal/input", {"sid": real_id, "data": prompt},
                    timeout=30)
    if not (res and res.get("ok")):
        return res
    time.sleep(_SUBMIT_DELAY)
    return _vps_post("/api/terminal/input", {"sid": real_id, "data": "\r"},
                     timeout=30)


def _read_remote_file(remote_file):
    """The file's text off the box, or None while it is not there yet.

    The endpoint answers a missing file with a JSON error body and a 200, so
    "did I get bytes" is not enough of a test - a brief never starts with a
    JSON object, which is what tells the two apart."""
    data = _vps_get_bytes(
        "/api/terminal/file?path=" + urllib.request.quote(remote_file))
    if not data:
        return None
    head = data.lstrip()[:1]
    if head == b"{" and b'"error"' in data[:200]:
        return None
    return data.decode("utf-8", "replace")


def _await_remote_brief(remote_file, timeout=BRIEF_TIMEOUT):
    """Wait for the brief to appear and stop growing, so a half-written file is
    never acted on. Returns its text, or None on timeout."""
    deadline = time.time() + timeout
    last_len, stable = -1, 0
    while time.time() < deadline:
        text = _read_remote_file(remote_file)
        if text is None:
            time.sleep(BRIEF_POLL)
            continue
        stable = stable + 1 if len(text) == last_len else 0
        last_len = len(text)
        if stable >= 2:
            return text.strip() or None
        time.sleep(BRIEF_POLL)
    return None


def _parse_handoff_header(text):
    """(header dict, body) from a brief. A missing header is not fatal - the
    prose is still worth having - so anything absent comes back empty."""
    header, lines = {}, text.splitlines()
    body_from = 0
    for i, line in enumerate(lines[:8]):
        if not line.strip():
            if header:
                body_from = i + 1
                break
            continue
        key, sep, val = line.partition(":")
        key = key.strip().upper()
        if sep and key in _HEADER_KEYS:
            val = val.strip()
            header[key] = "" if val in ("-", "<none>", "none") else val
            body_from = i + 1
    return header, "\n".join(lines[body_from:]).strip()


def _git(cwd, *args, timeout=180):
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _pull_box_work(local_cwd, branch, sha):
    """Fetch until the box's commit is here, then fast-forward if it is safe.

    Two separate things, deliberately. Fetching is always safe and is what
    actually brings the files across, so it happens whatever state the tree is
    in. Merging is not: a dirty tree or a different branch means the user has
    something of their own here, and quietly moving their HEAD would be the
    kind of help nobody asked for. So that case fetches, says so, and leaves
    the decision to the session that is about to open.

    Returns a sentence describing what happened, for the brief and the UI."""
    if not os.path.isdir(local_cwd):
        return "%s does not exist on this Mac, so nothing was pulled." % local_cwd

    inside = _git(local_cwd, "rev-parse", "--is-inside-work-tree", timeout=20)
    if not (inside and inside.returncode == 0):
        return "%s is not a git repo, so nothing was pulled." % local_cwd

    fetched = _git(local_cwd, "fetch", "--all", "--prune")
    if not (fetched and fetched.returncode == 0):
        return "git fetch failed here (%s) - the box's work is not on this Mac yet." % (
            (fetched.stderr or "").strip().splitlines()[-1] if fetched and fetched.stderr
            else "no network?")

    if not sha:
        return ("Fetched. The box named no commit, so nothing was merged - "
                "check what is on the branch before trusting the file list.")

    # The push and this fetch are two machines racing; give the commit a while
    # to turn up rather than declaring it missing on the first miss.
    deadline = time.time() + PULL_WAIT
    have = False
    while True:
        probe = _git(local_cwd, "cat-file", "-e", sha + "^{commit}", timeout=20)
        have = bool(probe and probe.returncode == 0)
        if have or time.time() > deadline:
            break
        time.sleep(PULL_POLL)
        _git(local_cwd, "fetch", "--all", "--prune")

    if not have:
        return ("Commit %s never arrived here within %ds - the box may not have "
                "finished pushing. Run git fetch and check before relying on "
                "the files." % (sha[:12], PULL_WAIT))

    dirty = _git(local_cwd, "status", "--porcelain", timeout=30)
    dirty_n = len([l for l in (dirty.stdout or "").splitlines() if l.strip()]) \
        if dirty else 0
    head = _git(local_cwd, "rev-parse", "--abbrev-ref", "HEAD", timeout=20)
    here = (head.stdout or "").strip() if head else ""

    if dirty_n:
        return ("Commit %s is on this Mac, but the local tree has %d "
                "uncommitted path(s), so it was NOT merged. The files are in "
                "git - merge or check them out when you have dealt with those."
                % (sha[:12], dirty_n))
    if branch and here and branch != here:
        return ("Commit %s is on this Mac. This clone is on %s, not %s, so it "
                "was not merged - switch branch if that is where the work "
                "should land." % (sha[:12], here, branch))

    merged = _git(local_cwd, "merge", "--ff-only", sha, timeout=120)
    if merged and merged.returncode == 0:
        return "Fast-forwarded %s to %s - the box's files are here." % (
            here or "this clone", sha[:12])
    return ("Commit %s is on this Mac but would not fast-forward (%s), so the "
            "working tree is unchanged." % (
                sha[:12],
                (merged.stderr or "").strip().splitlines()[-1] if merged and merged.stderr
                else "diverged"))


def _seed_local_session(sid, brief):
    """Plant the brief on this Mac and open Claude on it.

    Same shape as the outbound seed and for the same reason: the text goes to a
    file and the shell reads it, so prose full of quotes and backticks is never
    parsed by anything."""
    path = _brief_path(sid)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(brief)
        os.chmod(path, 0o600)
    except OSError as e:
        return {"ok": False, "error": "could not write the brief locally: %s" % e}
    return terminal_manager.write_session(
        sid, 'B="$(cat %s)"; rm -f %s; claude "$B"\r' % (path, path))


def bring_vps_to_local(sid, confirm=False, name=None):
    """Hand a box session's work over to a new session on this Mac."""
    if not _vps_enabled():
        return {"ok": False, "error": "vps not configured"}

    host, real = _split_sid(sid)
    if host != "vps":
        return {"ok": False, "error": "that session is already on this Mac"}

    online, vps_sessions, _states, _plans, _files = _vps_snapshot()
    sess = next((s for s in vps_sessions if s.get("realid") == real), None)
    if not sess:
        return {"ok": False, "error": "no such session on the box"}
    if not online:
        return {"ok": False, "error": "the box is not answering"}

    vps_cwd, vps_repo = _vps_session_place(real)
    if not vps_cwd:
        return {"ok": False, "error":
                "could not work out where that session is on the box"}
    local_cwd = _map_path_from_vps(vps_cwd)
    if not local_cwd:
        return {"ok": False, "error":
                "no local path is mapped for %s - add one under "
                "Settings -> VPS -> Folder mapping" % vps_cwd}

    # A local tree with uncommitted work cannot be fast-forwarded onto, so say
    # so before spending a minute of the box's Claude on a brief.
    dirty = _git_dirty(local_cwd)
    if dirty and not confirm:
        return {"ok": False, "needs_confirm": True, "cwd": local_cwd,
                "vps_cwd": vps_cwd, "dirty": dirty[:40],
                "dirty_total": len(dirty)}

    # Only ask for a brief when Claude actually owns the prompt over there; a
    # plain shell has no conversation to hand over, just a folder.
    cmd = (sess.get("cmd") or "").strip().lower()
    running_claude = (cmd in terminal_manager._AMBIGUOUS_RUNTIME
                      or terminal_manager._looks_like_version(cmd))

    header, body, sync = {}, "", ""
    if running_claude:
        remote_file = _remote_brief_path(vps_repo, vps_cwd, real)
        typed = _request_remote_brief(real, remote_file)
        if not (typed and typed.get("ok")):
            return {"ok": False, "error": "could not prompt the box's session"}
        text = _await_remote_brief(remote_file)
        if not text:
            return {"ok": False, "error":
                    "the box's session did not produce a brief within %ds - it "
                    "may be mid-task, waiting on a permission prompt, or its "
                    "folder may be outside what its terminal can serve"
                    % BRIEF_TIMEOUT}
        header, body = _parse_handoff_header(text)
        if header.get("CWD"):
            # Trust the session over the diff endpoint: it knows where it is.
            mapped = _map_path_from_vps(header["CWD"].rstrip("/"))
            if mapped:
                local_cwd = mapped
        sync = _pull_box_work(local_cwd, header.get("BRANCH", ""),
                              header.get("COMMIT", ""))
    else:
        sync = _pull_box_work(local_cwd, "", "")

    if not os.path.isdir(local_cwd):
        return {"ok": False, "error": "%s does not exist on this Mac" % local_cwd}

    started = terminal_manager.start_session(
        name or ("%s (VPS handover)" % sess.get("name", "session")),
        cwd=local_cwd)
    if not (started and started.get("ok") and started.get("session")):
        return {"ok": False, "error": started.get("error") if started
                else "could not start a local session"}
    new = started["session"]

    if body:
        opening = (
            "This work was handed over from a session on our VPS (\"%s\").\n\n"
            "On the box it lives at %s; on this Mac it is %s, which is where "
            "you are.\n\nGit: %s\n\nThe brief follows.\n\n---\n\n%s"
            % (sess.get("name", "session"), vps_cwd, local_cwd, sync, body))
        seeded = _seed_local_session(new["id"], opening)
        if not (seeded and seeded.get("ok")):
            return {"ok": True, "session": new, "cwd": local_cwd, "sync": sync,
                    "briefed": False,
                    "warning": "session started, but the brief did not land"}
    else:
        terminal_manager.write_session(new["id"], "cd %s\r" % local_cwd)

    return {"ok": True, "session": new, "cwd": local_cwd, "vps_cwd": vps_cwd,
            "sync": sync, "briefed": bool(body),
            "pushed": (header.get("PUSHED", "") or "").lower().startswith("y")}


# ── First-run setup ────────────────────────────────────────────────────────
# The app ships with no folder, no login and no VPS. Everything below is what
# the setup screen (and later the settings panel) drives to fill that in, so a
# copy of the .app handed to someone else starts as *their* app.

def apply_home(path):
    """Point new sessions at `path` immediately, no restart.

    terminal_manager fixes START_DIR at import time from TERMINAL_HOME, so both
    are set: the env var for anything imported later, the attribute for the copy
    already in memory.
    """
    os.environ["TERMINAL_HOME"] = path
    if terminal_manager is not None:
        terminal_manager.START_DIR = path


def setup_save_folder(raw):
    """Validate and store the working directory, and start using it."""
    path, err = setup.validate_folder(raw)
    if err:
        return {"ok": False, "error": err}
    setup.save_config({"home": path})
    apply_home(path)
    return {"ok": True, "state": setup.state()}


def setup_start_login(switch=False):
    """Open a real session and run `claude auth login` in it.

    Deliberately the same flow as signing in from Terminal.app: this server
    never sees an email, a password or a token - it types a command into a shell
    and the browser handshake happens between Claude Code and Anthropic. The
    session it returns is the one the setup screen attaches its terminal to, and
    the one the user keeps working in afterwards.

    The command is typed from a thread after a short beat, because a login shell
    that is still drawing its prompt eats input written the instant it is born.
    """
    cmd = setup.login_command(switch=switch)
    if not cmd:
        return {"ok": False,
                "error": "Claude Code is not installed on this Mac."}

    started = terminal_manager.start_session(
        "Switch account" if switch else "Sign in to Claude")
    if not started.get("ok"):
        return started
    sid = started["session"]["id"]
    setup.forget_auth_cache()   # so the next poll asks Claude Code, not the cache

    def _type_command():
        time.sleep(0.7)
        terminal_manager.write_session(sid, cmd + "\r")

    threading.Thread(target=_type_command, daemon=True).start()
    return {"ok": True, "sid": sid, "session": started["session"]}


def setup_complete(login_sid=None):
    """Finish setup. Refuses unless there is a usable folder and a real login -
    the whole point is that the app runs on the user's own subscription.

    A session that was opened to sign in is kept, renamed after the folder: it is
    already sitting in the right directory with Claude authenticated, so it
    becomes the user's first working session rather than a leftover.
    """
    st = setup.state(force_auth=True)
    if not st["home"]["valid"]:
        return {"ok": False, "error": "Choose a folder first.", "state": st}
    if not st["claude"]["loggedIn"]:
        return {"ok": False, "error": "Sign in to Claude first.", "state": st}

    setup.save_config({"setupComplete": True})
    if login_sid:
        terminal_manager.rename_session(login_sid, st["home"]["name"] or "Session")
    return {"ok": True, "state": setup.state()}


def _probe_vps(base, token):
    """(True, "") if `base` answers the dashboard terminal API with this token."""
    try:
        req = urllib.request.Request(base + "/api/terminal/sessions",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "The dashboard rejected that token."
        return False, "The dashboard answered HTTP %s." % e.code
    except Exception as e:
        return False, "Could not reach %s (%s)." % (base, e)
    if not (isinstance(data, dict) and data.get("ok")):
        return False, "That address answered, but not like an AI-Hub dashboard."
    return True, ""


def setup_save_vps(base_url, token, path_map):
    """Connect, re-point or disconnect the optional VPS aggregation.

    An empty base url disconnects. A blank token means "keep the one you have",
    because the token is never sent to the page and so cannot be echoed back by
    the form. Nothing is stored until the box actually answers with it.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        setup.save_config({"vps": None})
        _load_vps_config()
        return {"ok": True, "state": setup.state()}

    if not base.startswith(("http://", "https://")):
        base = "http://" + base

    tok = (token or "").strip()
    if not tok:
        current = setup.load_config().get("vps")
        tok = (current or {}).get("token", "") if isinstance(current, dict) else ""
    if not tok:
        return {"ok": False, "error": "A dashboard token is required."}

    ok, err = _probe_vps(base, tok)
    if not ok:
        return {"ok": False, "error": err}

    pm = {str(k): str(v) for k, v in path_map.items()} \
        if isinstance(path_map, dict) else {}
    setup.save_config({"vps": {"base_url": base, "token": tok, "path_map": pm}})
    _load_vps_config()
    return {"ok": True, "state": setup.state()}


# ── How the app is behaving, in numbers ────────────────────────────────────
# "It gets buggy after an hour and I am not sure what makes it happen" is not a
# thing you can fix by reading code harder. These are the figures that would
# have answered it: how long the polls are actually taking, and what this
# process is holding open while they do.
_STARTED = time.time()
_timings = {}
_timings_lock = threading.Lock()
_TIMING_KEEP = 120


def _note_timing(kind, secs):
    with _timings_lock:
        series = _timings.setdefault(kind, [])
        series.append(secs)
        if len(series) > _TIMING_KEEP:
            del series[:len(series) - _TIMING_KEEP]


def _timing_summary():
    out = {}
    with _timings_lock:
        snapshot = {k: list(v) for k, v in _timings.items()}
    for kind, series in snapshot.items():
        if not series:
            continue
        ordered = sorted(series)
        out[kind] = {
            "n": len(ordered),
            "p50": round(ordered[len(ordered) // 2], 3),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
            "max": round(ordered[-1], 3),
        }
    return out


def health():
    stats = terminal_manager.view_stats() if terminal_manager else {}
    online, vps_sessions, _st, _pl, _fi = _vps_snapshot()
    with _vps_snap_lock:
        age = time.time() - _vps_snap["ts"] if _vps_snap["ts"] else None
    return {
        "ok": True,
        "uptime": round(time.time() - _STARTED, 1),
        "held": stats,
        "timings": _timing_summary(),
        "vps": {"enabled": _vps_enabled(), "online": online,
                "sessions": len(vps_sessions),
                "snapshot_age": round(age, 1) if age is not None else None},
    }


CLIP_LOG = os.path.join(os.path.expanduser("~"), "Library", "Logs",
                        "mattdaemon-clip.log")
_CLIP_LOG_MAX = 512 * 1024


def _clip_debug(data):
    """Append one copy-attempt event. Truncates rather than rotates: this is a
    breadcrumb trail for the last failure, not an archive."""
    try:
        if os.path.getsize(CLIP_LOG) > _CLIP_LOG_MAX:
            os.remove(CLIP_LOG)
    except OSError:
        pass
    os.makedirs(os.path.dirname(CLIP_LOG), exist_ok=True)
    with open(CLIP_LOG, "a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (time.strftime("%H:%M:%S"),
                              json.dumps(data, ensure_ascii=False)[:2000]))


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

    def _blocked_by_setup(self):
        """True (and a 403 already sent) when setup has not been finished.

        The page gates itself, so this is the backstop: nothing may open a shell
        on this machine until a folder has been chosen and a Claude login has
        been established. The sign-in session itself is started server-side by
        /api/setup/login, which is why that route bypasses this.
        """
        if setup.is_complete():
            return False
        self._json(403, {"ok": False, "needsSetup": True,
                         "error": "Finish setting up Mattdaemon first."})
        return True

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

        if path == "/api/setup/state":
            qs = parse_qs(urlparse(self.path).query)
            force = qs.get("force", [""])[0] in ("1", "true", "yes")
            self._json(200, setup.state(force_auth=force))
            return
        if path == "/api/setup/browse":
            qs = parse_qs(urlparse(self.path).query)
            self._json(200, setup.list_dirs(qs.get("path", ["~"])[0]))
            return

        if path == "/api/terminal/stream":
            self._stream_terminal()
            return
        if path == "/api/terminal/stream-multi":
            self._stream_multi()
            return
        if path == "/api/terminal/sessions":
            t0 = time.time()
            payload = _merged_sessions()
            _note_timing("sessions", time.time() - t0)
            self._json(200, payload)
            return
        if path == "/api/terminal/states":
            t0 = time.time()
            payload = _merged_states()
            _note_timing("states", time.time() - t0)
            self._json(200, payload)
            return
        if path == "/api/terminal/health":
            self._json(200, health())
            return
        if path == "/api/terminal/worktrees":
            # dirty=1 costs a `git status` per worktree, so the sidebar's poll
            # leaves it off and only a deliberate check asks for it.
            qs = parse_qs(urlparse(self.path).query)
            want_dirty = (qs.get("dirty", [""])[0] or "") == "1"
            self._json(200, _merged_worktrees(with_dirty=want_dirty))
            return
        if path == "/api/terminal/worktree/status":
            qs = parse_qs(urlparse(self.path).query)
            wid = (qs.get("id", [""])[0] or "").strip()
            if wid.startswith(SID_VPS_PREFIX):
                if not _vps_enabled():
                    self._json(404, {"ok": False, "error": "vps not configured"})
                    return
                self._json(200, _vps_get("/api/terminal/worktree/status?id=%s"
                                         % _split_sid(wid)[1]) or
                           {"ok": False, "error": "vps unreachable"})
                return
            self._json(200, terminal_manager.worktree_status(wid))
            return
        if path == "/api/terminal/worktree/stale":
            self._json(200, terminal_manager.stale_worktree_branches())
            return
        if path == "/api/terminal/repos":
            self._json(200, _merged_repos())
            return
        if path == "/api/terminal/github/repos":
            # This Mac's credential store only. Nothing of the token reaches the
            # page - it is read, used, and dropped inside this process.
            self._json(200, terminal_manager.github_repos())
            return
        if path == "/api/terminal/plan":
            self._serve_plan()
            return
        if path == "/api/terminal/file":
            self._serve_artifact()
            return

        self._json(404, {"error": "not found"})

    def _serve_artifact(self):
        """Serve a file a session produced, for the browser to show.

        The path arrives either from a session's @webfile stamp (shared/term-show)
        or from a term-link URL clicked in the terminal. Either way the page
        hands this URL to your default browser rather than opening it itself. A local file must resolve
        inside the repo the app was started on - the terminal can reach the whole
        disk, but this endpoint has no reason to. A vps: session's file lives on
        the box, so that one is fetched from the box's own endpoint, which also
        keeps its auth token out of the page."""
        qs = parse_qs(urlparse(self.path).query)
        rel = (qs.get("path", [""])[0] or "").strip()

        if qs.get("host", [""])[0] == "vps":
            data = _vps_get_bytes(
                "/api/terminal/file?path=" + urllib.request.quote(rel))
            if data is None:
                self._json(502, {"ok": False, "error": "file unreachable on the box"})
                return
            self.send_response(200)
            self.send_header("Content-Type", _ctype_for(rel))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        root = os.path.realpath(terminal_manager.START_DIR)
        # An absolute path wins the join, which is what @webfile stamps carry.
        target = os.path.realpath(os.path.join(root, rel))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            self._json(404, {"ok": False, "error": "no such file under " + root})
            return
        self._serve_file(target, _ctype_for(target))

    def _serve_plan(self):
        """Serve a visual-plan HTML file, for the browser to show.

        The path comes from a session's @webplan stamp, written by
        shared/term-plan when /farm produces a plan, and is restricted to
        *.html under the repo's plans/ directory. A vps: session's plan lives
        on the box, so that one is fetched from the same endpoint there rather
        than looked for locally - the Mac clone may not have it at all."""
        qs = parse_qs(urlparse(self.path).query)
        rel = (qs.get("path", [""])[0] or "").strip()

        if qs.get("host", [""])[0] == "vps":
            data = _vps_get_bytes(
                "/api/terminal/plan?path=" + urllib.request.quote(rel))
            if data is None:
                self._json(502, {"ok": False, "error": "plan unreachable on the box"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        root = terminal_manager.START_DIR
        plans_root = os.path.realpath(os.path.join(root, "plans"))
        target = os.path.realpath(os.path.join(root, rel))
        if (not target.startswith(plans_root + os.sep)
                or not target.endswith(".html")
                or not os.path.isfile(target)):
            self._json(404, {"ok": False, "error": "not a plan file"})
            return
        self._serve_file(target, "text/html; charset=utf-8")

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
            if self._blocked_by_setup():
                return
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

    # How many sessions one merged connection will carry. The grid tops out at
    # four panes; the slack is for a pop-out window's session still being listed
    # while the grid rearranges itself around its absence.
    MULTI_MAX = 8

    def _stream_multi(self):
        """Stream several sessions down one connection, each event tagged.

        The grid shows up to four sessions at once and a browser gives an origin
        about six connections in total. One stream per pane would spend nearly
        all of them on output, leaving the polls and - worse - the per-keystroke
        POSTs queued behind them, which reads as a terminal that lags while you
        type. Pop-out windows make it certain rather than likely: they are more
        pages against the same cap. So the panes share one connection.

        Each session gets its own pump thread rather than being read round-robin,
        because the per-session generators block: one quiet session would
        otherwise hold up every busy one behind it.
        """
        qs = parse_qs(urlparse(self.path).query)
        raw = qs.get("sids", [""])[0]
        sids, seen = [], set()
        for sid in (raw.split(",") if raw else []):
            sid = sid.strip()
            # Dedupe: the same session in two panes would mean two views of one
            # shell competing over its size.
            if sid and sid not in seen:
                seen.add(sid)
                sids.append(sid)
        sids = sids[:self.MULTI_MAX]
        if not sids:
            self._json(400, {"error": "no sids"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        merged = queue.Queue(maxsize=4096)
        stop = threading.Event()

        def pump(sid):
            """One session into the shared queue until told to stop."""
            source = _session_event_source(sid)
            try:
                for event in source:
                    if stop.is_set():
                        break
                    # Never block for good on a full queue: if the client has
                    # gone, `stop` is the only thing that will ever free it.
                    while not stop.is_set():
                        try:
                            merged.put((sid, event), timeout=0.5)
                            break
                        except queue.Full:
                            continue
            except Exception:
                pass
            finally:
                source.close()
                try:
                    merged.put((sid, None), timeout=0.5)
                except queue.Full:
                    pass

        for sid in sids:
            threading.Thread(target=pump, args=(sid,), daemon=True).start()

        live = set(sids)
        try:
            # Say what we actually attached to. The client asked for a set and a
            # cap may have trimmed it, and it needs to know which panes are now
            # its responsibility to leave blank.
            self.wfile.write(
                f"data: {json.dumps({'type': 'attached', 'sids': sids})}\n\n".encode())
            self.wfile.flush()
            while live:
                try:
                    sid, event = merged.get(timeout=1.0)
                except queue.Empty:
                    continue
                if event is None:
                    # Say so. While any other pane is still live the response
                    # stays open, so a browser has no way to notice that one of
                    # its panes has gone quiet for good: it keeps a healthy
                    # connection and a frozen screen. Told, it can re-open the
                    # stream and get that pane a fresh attach.
                    live.discard(sid)
                    self.wfile.write(
                        f"data: {json.dumps({'type': 'source-ended', 'sid': sid})}\n\n".encode())
                    self.wfile.flush()
                    continue
                self.wfile.write(
                    f"data: {json.dumps(dict(event, sid=sid))}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; the tmux sessions stay alive
        finally:
            # Releases the pump threads, and with them the views they subscribe
            # to. A thread parked inside a session generator notices on that
            # generator's next yield, so a quiet session can take up to a ping
            # interval to let go - harmless, since a view tolerates subscribers
            # coming and going.
            stop.set()

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

        # ── setup ──
        # These are the only POSTs that work before setup is finished; they are
        # what finishes it.
        if path == "/api/setup/folder":
            self._json(200, setup_save_folder(data.get("path", "")))
            return
        if path == "/api/setup/login":
            self._json(200, setup_start_login(switch=bool(data.get("switch"))))
            return
        if path == "/api/setup/complete":
            self._json(200, setup_complete(data.get("sid") or None))
            return
        if path == "/api/setup/vps":
            self._json(200, setup_save_vps(data.get("base_url", ""),
                                           data.get("token", ""),
                                           data.get("path_map")))
            return
        if path == "/api/setup/reset":
            # Hand the app to someone else, or just re-run setup: the folder and
            # any VPS block stay put, only the "finished" flag is dropped. The
            # Claude login is Claude Code's own and is never touched from here.
            setup.save_config({"setupComplete": False})
            self._json(200, {"ok": True, "state": setup.state()})
            return

        # /start has no sid yet, so it routes on an explicit host field; every
        # other control call routes on its sid's vps: prefix.
        if path == "/api/terminal/start":
            if self._blocked_by_setup():
                return
            # autostart is whatever the caller asked for, and callers who say
            # nothing get a plain shell. The button in the UI asks for claude;
            # the selftests and the handover want a shell they can type into,
            # and a default of ON silently broke both.
            auto = bool(data.get("autostart"))
            agent = data.get("agent") if auto else None
            if data.get("host") == "vps":
                res = _vps_start(data.get("name"), auto, agent)
                if res.get("ok") and res.get("session"):
                    _vps_snap_add([res["session"]])
                self._json(200, res)
                return
            # A name we do not know is the caller's mistake, and the only
            # failure here that is: everything else start_session can refuse is
            # a state of this machine, which it has always answered 200 with.
            if auto and terminal_manager.agent_spec(agent) is None:
                self._json(400, {"ok": False,
                                 "error": "unknown agent: %s" % agent})
                return
            self._json(200, terminal_manager.start_session(
                data.get("name"), autostart=auto, agent=agent))
            return

        if path == "/api/terminal/github/clone":
            # Local only: cloning uses this machine's git credentials, and the
            # box has none of its own.
            if self._blocked_by_setup():
                return
            result = terminal_manager.clone_repo(
                data.get("repo", ""), data.get("dest"), data.get("name"))
            self._json(200 if result.get("ok") else 400, result)
            return

        # Worktree calls route on a host, like /start does, and are handled
        # before the vps: sid split below because they carry a worktree id
        # rather than a session id. A create says which machine outright; add
        # and remove carry a namespaced group id, so the prefix answers for them
        # even if the client forgot to say.
        if path == "/api/terminal/worktree/finish":
            wid = str(data.get("id") or "")
            if wid.startswith(SID_VPS_PREFIX) or data.get("host") == "vps":
                if not _vps_enabled():
                    self._json(404, {"ok": False, "error": "vps not configured"})
                    return
                body = dict(data); body.pop("host", None)
                body["id"] = _split_sid(wid)[1]
                code, res = _vps_post_full(path, body, timeout=900)
                self._json(code or 502, res)
                return
            result = terminal_manager.finish_worktree(
                wid, data.get("message"), bool(data.get("pr", True)))
            self._json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/terminal/worktree/prune-branches":
            result = terminal_manager.delete_worktree_branches(
                data.get("repo", ""), data.get("branches") or [])
            self._json(200 if result.get("ok") else 400, result)
            return

        if path in ("/api/terminal/worktree/create",
                    "/api/terminal/worktree/add-session",
                    "/api/terminal/worktree/remove"):
            wid = str(data.get("id") or "")
            host = data.get("host") or ("vps" if wid.startswith(SID_VPS_PREFIX) else "local")

            if host == "vps":
                if not _vps_enabled():
                    self._json(404, {"ok": False, "error": "vps not configured"})
                    return
                body = dict(data)
                body.pop("host", None)
                if wid:
                    body["id"] = _split_sid(wid)[1]   # the box knows its own id
                # VPS_TIMEOUT is six seconds, which is right for a keystroke and
                # far too short for git. `git worktree add` on a twelve-thousand
                # file checkout takes tens of seconds, and a client that gives up
                # first does NOT stop the box: it finishes, and you are left with
                # a worktree and its shells that nothing on this side asked to
                # keep. Removal has the same shape - it kills sessions and
                # deletes a checkout before it can answer.
                code, res = _vps_post_full(path, body, timeout=_WT_TIMEOUTS[path])
                # Namespace whatever came back, so the frontend can switch
                # straight to the new pane and match it to the merged listing.
                if res.get("ok"):
                    if res.get("worktree", {}).get("id"):
                        res["worktree"] = dict(res["worktree"])
                        res["worktree"]["realid"] = res["worktree"]["id"]
                        res["worktree"]["id"] = SID_VPS_PREFIX + res["worktree"]["id"]
                        res["worktree"]["host"] = "vps"
                    for s in res.get("sessions", []) or []:
                        s["realid"] = s.get("id", "")
                        s["id"] = SID_VPS_PREFIX + s.get("id", "")
                        s["host"] = "vps"
                    if res.get("session"):
                        res["session"] = dict(res["session"])
                        res["session"]["realid"] = res["session"].get("id", "")
                        res["session"]["id"] = SID_VPS_PREFIX + res["session"].get("id", "")
                        res["session"]["host"] = "vps"
                    # The box holds these shells NOW. Without this the frontend
                    # opens a pane on a session its own listing will not admit
                    # exists for another poll or two, which is most of what
                    # "adding a session to a worktree is slow" was.
                    _vps_snap_add(res.get("sessions") or
                                  ([res["session"]] if res.get("session") else []))
                self._json(code if code else 502, res)
                return

            if path == "/api/terminal/worktree/create":
                if self._blocked_by_setup():
                    return
                result = terminal_manager.create_worktree_group(
                    data.get("name", ""), data.get("base"), data.get("count", 1),
                    bool(data.get("autostart")), data.get("repo"),
                    data.get("agent"))
                self._json(200 if result.get("ok") else 400, result)
                return
            if path == "/api/terminal/worktree/add-session":
                if self._blocked_by_setup():
                    return
                result = terminal_manager.add_session_to_worktree(
                    wid, data.get("name"), bool(data.get("autostart")),
                    data.get("agent"))
                self._json(200 if result.get("ok") else 400, result)
                return
            # A removal refusal is a 409, not a 400: the request was well formed
            # and the client is expected to ask again with force.
            result = terminal_manager.remove_worktree_group(wid, bool(data.get("force")))
            code = 200 if result.get("ok") else (409 if result.get("dirty") else 400)
            self._json(code, result)
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

        # The same in reverse, and routed here for the same reason: it drives
        # both machines, and it waits - on the box's Claude, then on git.
        if path == "/api/terminal/bring-from-vps":
            self._json(200, bring_vps_to_local(
                data.get("sid", ""), confirm=bool(data.get("confirm")),
                name=data.get("name")))
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
            res = _vps_post(path, body, timeout=tmo)
            if path == "/api/terminal/stop" and res.get("ok"):
                _vps_snap_drop([data.get("sid", "")])
            self._json(200, res)
            return

        if path == "/api/terminal/clipdebug":
            # A copy that fails leaves nothing behind to look at: the toast is
            # gone in three seconds and the page's console dies with the window.
            # This writes one line per step of a copy attempt to a file, so a
            # report of "it still does not work" can be answered by reading
            # rather than by guessing.
            try:
                _clip_debug(data)
            except Exception:
                pass
            self._json(200, {"ok": True})
            return

        if path == "/api/terminal/input":
            self._json(200, terminal_manager.write_session(
                data.get("sid", ""), data.get("data", "")))
            return
        if path == "/api/terminal/resize":
            self._json(200, terminal_manager.resize_session(
                data.get("sid", ""), data.get("cols", 80), data.get("rows", 24),
                data.get("viewer", "")))
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
    import tempfile
    import time
    import urllib.request

    # Opening a shell is gated on setup being finished, so the selftest gets a
    # throwaway config that says it is. It is never the machine's real one, and
    # the folder it names is this test's own.
    workdir = tempfile.mkdtemp(prefix="mts-selftest-")
    os.environ["MTS_CONFIG"] = os.path.join(workdir, "mts-config.json")
    setup.save_config({"setupComplete": True, "home": workdir})
    os.environ["TERMINAL_HOME"] = workdir

    tm = _import_manager()
    tm.START_DIR = workdir
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


def _run_multi_selftest():
    """Prove the grid's transport: two shells, one connection, no crossed wires.

    The merged stream is the thing the whole grid rests on, and the failure that
    matters is not "no output" - it is output arriving under the wrong sid, which
    on screen looks like one session typing into another's pane. So this drives
    two real tmux shells at once down a single connection and asserts each token
    comes back tagged with the session that produced it, and never with the other.
    """
    import base64
    import tempfile
    import time
    import urllib.request

    workdir = tempfile.mkdtemp(prefix="mts-multitest-")
    os.environ["MTS_CONFIG"] = os.path.join(workdir, "mts-config.json")
    setup.save_config({"setupComplete": True, "home": workdir})
    os.environ["TERMINAL_HOME"] = workdir

    tm = _import_manager()
    tm.START_DIR = workdir
    if not tm._tmux_available():
        print("multi-selftest FAIL: tmux not available", file=sys.stderr)
        return 1

    httpd = _make_server(0)
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def _post(pth, obj):
        req = urllib.request.Request(
            base + pth, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    TOKENS = {}                      # sid -> the token only that shell prints
    hits = {}                        # sid -> set of sids it was seen tagged with
    done = threading.Event()
    sids = []

    def _reader():
        try:
            url = base + "/api/terminal/stream-multi?sids=" + ",".join(sids)
            with urllib.request.urlopen(url, timeout=30) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") not in ("output", "replay") or not ev.get("data"):
                        continue
                    tagged = ev.get("sid")
                    try:
                        text = base64.b64decode(ev["data"]).decode("utf-8", "replace")
                    except (ValueError, TypeError):
                        continue
                    for owner, token in TOKENS.items():
                        if token in text:
                            hits.setdefault(owner, set()).add(tagged)
                    if all(hits.get(s) for s in sids):
                        done.set()
                        return
        except Exception:
            pass

    ok = False
    try:
        for n in (1, 2):
            started = _post("/api/terminal/start", {"name": f"multitest{n}"})
            if not started.get("ok"):
                print(f"multi-selftest FAIL: start_session: {started}", file=sys.stderr)
                return 1
            sid = started["session"]["id"]
            sids.append(sid)
            TOKENS[sid] = f"multi-ok-{n}-token"

        threading.Thread(target=_reader, daemon=True).start()
        time.sleep(1.2)  # let both views attach and the shells settle

        for sid, token in TOKENS.items():
            typed = _post("/api/terminal/input", {"sid": sid, "data": "echo %s\n" % token})
            if not typed.get("ok"):
                print(f"multi-selftest FAIL: write_session: {typed}", file=sys.stderr)
                return 1

        if not done.wait(timeout=25):
            print(f"multi-selftest FAIL: tokens never both arrived (got {hits})",
                  file=sys.stderr)
            return 1

        # The real assertion: each token arrived tagged with its own session and
        # with nothing else. A merge that mislabels is worse than one that drops.
        ok = True
        for owner in sids:
            seen = hits.get(owner, set())
            if seen != {owner}:
                print(f"multi-selftest FAIL: {owner} output tagged {seen or 'nothing'}",
                      file=sys.stderr)
                ok = False
    finally:
        for sid in sids:
            try:
                _post("/api/terminal/stop", {"sid": sid})
            except Exception:
                pass
        httpd.shutdown()

    if ok:
        print("VERIFY_OK")
        return 0
    return 1


def _run_setup_selftest():
    """Drive the first-run setup API exactly as the setup screen does.

    Runs against a throwaway config (MTS_CONFIG), so the machine's real one is
    never touched, and needs neither tmux nor a network: it proves the gate, the
    folder validation, the config round-trip and the VPS save path. Whether the
    final "complete" call succeeds depends on this machine actually having a
    Claude login, so both outcomes are asserted, each against what it must mean.
    Prints VERIFY_OK / exits 0 on success.
    """
    import shutil
    import tempfile
    import urllib.request

    tmpdir = tempfile.mkdtemp(prefix="mts-setup-test-")
    os.environ["MTS_CONFIG"] = os.path.join(tmpdir, "config", "mts-config.json")
    workdir = os.path.join(tmpdir, "MyProject")
    os.makedirs(os.path.join(workdir, "sub"))
    setup.save_config({"setupComplete": False, "home": "", "vps": None})

    httpd = _make_server(0)
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    failures = []

    def check(cond, label):
        if not cond:
            failures.append(label)
        print(("  ok   " if cond else "  FAIL ") + label)

    def get(pth):
        with urllib.request.urlopen(base + pth, timeout=15) as r:
            return json.loads(r.read())

    def post(pth, obj):
        req = urllib.request.Request(
            base + pth, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        st = get("/api/setup/state")
        check(st.get("needsSetup") is True, "a fresh install needs setup")
        check(st["home"]["valid"] is False, "no folder is configured yet")

        code, body = post("/api/terminal/start", {})
        check(code == 403 and body.get("needsSetup") is True,
              "opening a shell is refused until setup is finished")

        _, body = post("/api/setup/folder", {"path": os.path.join(tmpdir, "nope")})
        check(body.get("ok") is False, "a folder that does not exist is rejected")

        _, body = post("/api/setup/folder", {"path": workdir})
        check(body.get("ok") is True, "a real folder is accepted")
        check(body["state"]["home"]["path"] == workdir, "the folder is stored")
        check(body["state"]["home"]["name"] == "MyProject", "its name is derived")
        check(os.environ.get("TERMINAL_HOME") == workdir,
              "new sessions are pointed at it without a restart")

        listing = get("/api/setup/browse?path=" + urllib.request.quote(workdir))
        check([d["name"] for d in listing.get("dirs", [])] == ["sub"],
              "the folder browser lists sub-folders")

        _, body = post("/api/setup/vps",
                       {"base_url": "http://127.0.0.1:9", "token": "x"})
        check(body.get("ok") is False, "a VPS that does not answer is not saved")
        check(not (setup.load_config().get("vps") or {}).get("base_url"),
              "and nothing was written for it")

        _, body = post("/api/setup/vps", {"base_url": "", "token": ""})
        check(body.get("ok") is True and body["state"]["vps"]["configured"] is False,
              "an empty address disconnects the VPS")

        signed_in = get("/api/setup/state")["claude"]["loggedIn"]
        _, body = post("/api/setup/complete", {})
        if signed_in:
            check(body.get("ok") is True, "setup completes once folder + login are in")
            check(get("/api/setup/state")["needsSetup"] is False,
                  "and the app stops asking")
            # Routed at the box (which is not configured, so it fails fast) -
            # this proves the gate opened without spawning a real shell.
            code, body = post("/api/terminal/start", {"host": "vps"})
            check(code != 403 and body.get("needsSetup") is None,
                  "the shell gate is open afterwards")
            _, body = post("/api/setup/reset", {})
            check(get("/api/setup/state")["needsSetup"] is True,
                  "reset puts the app back to first-run")
        else:
            check(body.get("ok") is False and "Sign in" in (body.get("error") or ""),
                  "setup refuses to finish without a Claude login")

        mode = os.stat(setup.user_config_path()).st_mode & 0o777
        check(mode == 0o600, "the config file is private (0600), it can hold a token")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    if failures:
        print("setup selftest FAIL: %d check(s) failed" % len(failures),
              file=sys.stderr)
        return 1
    print("VERIFY_OK")
    return 0


def _run_handover_selftest():
    """Prove the VPS -> Mac handover's own logic, without a VPS.

    The parts that can be wrong quietly are the ones tested here: the path map
    read backwards, the brief's header, telling a real file on the box from its
    endpoint's JSON "not found", and - the one that actually moves work between
    two machines - the git step. That last one runs against real repositories
    created for the test, so "fetched", "fast-forwarded" and "refused to merge
    into a dirty tree" are observed, not assumed. No tmux, no network.
    """
    import shutil
    import tempfile

    global VPS_PATH_MAP
    failures = []

    def check(cond, label):
        if not cond:
            failures.append(label)
        print(("  ok   " if cond else "  FAIL ") + label)

    tmpdir = tempfile.mkdtemp(prefix="mts-handover-test-")
    saved_map = VPS_PATH_MAP
    try:
        # ── the path map, read backwards ──
        VPS_PATH_MAP = {"~/Documents/AI-Hub": "/home/aihub/AI-Hub",
                        "~/Documents/AI-Hub/clients": "/home/aihub/clients"}
        home = os.path.expanduser("~")
        check(_map_path_from_vps("/home/aihub/AI-Hub") == home + "/Documents/AI-Hub",
              "a mapped box folder resolves to its Mac folder")
        check(_map_path_from_vps("/home/aihub/AI-Hub/shared/x.py")
              == home + "/Documents/AI-Hub/shared/x.py",
              "a file below a mapped folder keeps its tail")
        check(_map_path_from_vps("/home/aihub/clients/rm")
              == home + "/Documents/AI-Hub/clients/rm",
              "the longest matching prefix wins, not the first")
        check(_map_path_from_vps("/srv/elsewhere") is None,
              "an unmapped folder is refused rather than guessed at")
        # The inverse of the outbound map, or a session could round-trip into a
        # different folder than it started in.
        VPS_PATH_MAP = {"~/Documents/AI-Hub": "/home/aihub/AI-Hub"}
        there = _map_path_to_vps(home + "/Documents/AI-Hub/shared")
        check(_map_path_from_vps(there) == home + "/Documents/AI-Hub/shared",
              "out and back lands in the folder it started in")

        # ── the brief's header ──
        header, body = _parse_handoff_header(
            "CWD: /home/aihub/AI-Hub\nBRANCH: feat/x\nCOMMIT: abc123\n"
            "PUSHED: yes\n\nWe are building the thing.\n\nNext: ship it.")
        check(header.get("CWD") == "/home/aihub/AI-Hub"
              and header.get("BRANCH") == "feat/x"
              and header.get("COMMIT") == "abc123"
              and header.get("PUSHED") == "yes",
              "the four header lines are read")
        check(body.startswith("We are building") and "COMMIT" not in body,
              "the body starts after the header, not inside it")
        header, body = _parse_handoff_header(
            "CWD: /home/aihub/AI-Hub\nBRANCH: -\nCOMMIT: -\nPUSHED: no\n\nNo repo here.")
        check(header.get("BRANCH") == "" and header.get("COMMIT") == "",
              "a dash means 'nothing', not the string '-'")
        header, body = _parse_handoff_header("Just prose, no header at all.")
        check(header == {} and body.startswith("Just prose"),
              "a brief with no header is still a brief")

        # ── where the box is told to write ──
        check(_remote_brief_path("/home/aihub/AI-Hub", "/home/aihub/AI-Hub/x", "abc")
              == "/home/aihub/AI-Hub/.git/mts-handoff-abc.md",
              "the brief goes inside .git, where git never looks")
        check(_remote_brief_path("", "/home/aihub/scratch", "abc")
              == "/home/aihub/scratch/.mts-handoff-abc.md",
              "with no repo it is a dotfile in the folder instead")

        # ── git: the part that actually moves the work ──
        def git(cwd, *a):
            return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)

        origin = os.path.join(tmpdir, "origin.git")
        box = os.path.join(tmpdir, "box")
        mac = os.path.join(tmpdir, "mac")
        subprocess.run(["git", "init", "--bare", "-q", "-b", "main", origin],
                       capture_output=True)
        subprocess.run(["git", "clone", "-q", origin, box], capture_output=True)
        for repo in (box,):
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
        open(os.path.join(box, "README.md"), "w").write("one\n")
        git(box, "add", "-A"); git(box, "commit", "-qm", "first")
        git(box, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", origin, mac], capture_output=True)
        git(mac, "config", "user.email", "test@example.com")
        git(mac, "config", "user.name", "Test")

        # The box builds something and pushes it, exactly as the handover asks.
        open(os.path.join(box, "feature.py"), "w").write("print('built on the box')\n")
        git(box, "add", "-A"); git(box, "commit", "-qm", "work from the box")
        git(box, "push", "-q", "origin", "main")
        sha = git(box, "rev-parse", "HEAD").stdout.strip()

        check(not os.path.exists(os.path.join(mac, "feature.py")),
              "the Mac has not got the box's file yet")
        sync = _pull_box_work(mac, "main", sha)
        check("Fast-forwarded" in sync, "a clean clone fast-forwards: " + sync)
        check(os.path.exists(os.path.join(mac, "feature.py")),
              "and the box's file is now really on the Mac")

        # Now the same thing into a tree the user has been working in.
        open(os.path.join(box, "second.py"), "w").write("print('more')\n")
        git(box, "add", "-A"); git(box, "commit", "-qm", "more work")
        git(box, "push", "-q", "origin", "main")
        sha2 = git(box, "rev-parse", "HEAD").stdout.strip()
        open(os.path.join(mac, "mine.txt"), "w").write("my own uncommitted work\n")

        sync = _pull_box_work(mac, "main", sha2)
        check("NOT be merged" in sync or "NOT merged" in sync,
              "a dirty tree is fetched but not merged: " + sync)
        have = git(mac, "cat-file", "-e", sha2 + "^{commit}")
        check(have.returncode == 0, "the commit is on the Mac all the same")
        check(not os.path.exists(os.path.join(mac, "second.py")),
              "and the working tree was left exactly as the user had it")
        check(open(os.path.join(mac, "mine.txt")).read().startswith("my own"),
              "the user's own uncommitted file is untouched")

        # A commit that was never pushed must be reported, not waited on for ever.
        saved_wait = globals()["PULL_WAIT"]
        globals()["PULL_WAIT"] = 0
        try:
            sync = _pull_box_work(mac, "main", "0" * 40)
            check("never arrived" in sync, "an unpushed commit is reported: " + sync)
        finally:
            globals()["PULL_WAIT"] = saved_wait

        sync = _pull_box_work(os.path.join(tmpdir, "not-a-repo"), "main", sha)
        check("does not exist" in sync, "a missing local folder is reported")
    finally:
        VPS_PATH_MAP = saved_map
        shutil.rmtree(tmpdir, ignore_errors=True)

    if failures:
        print("handover selftest FAIL: %d check(s) failed" % len(failures),
              file=sys.stderr)
        return 1
    print("VERIFY_OK")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Standalone local terminal server")
    parser.add_argument("--port", type=int, default=8722)
    parser.add_argument("--home", default=None,
                        help="directory new terminal sessions start in")
    parser.add_argument("--vps-url", default=None,
                        help="base url of the :3333 dashboard to aggregate "
                             "(e.g. http://192.0.2.10:3333)")
    parser.add_argument("--vps-token", default=None,
                        help="dashboard Bearer token for --vps-url")
    parser.add_argument("--selftest", action="store_true",
                        help="round-trip a token through a real tmux shell, then exit")
    parser.add_argument("--selftest-setup", action="store_true",
                        help="exercise the first-run setup API against a throwaway "
                             "config, then exit (no tmux, no network)")
    parser.add_argument("--selftest-multi", action="store_true",
                        help="drive two real shells down one merged stream and "
                             "assert each one's output is tagged with its own "
                             "session, then exit")
    parser.add_argument("--selftest-handover", action="store_true",
                        help="exercise the VPS -> Mac handover's path map, brief "
                             "header and git step against real throwaway repos, "
                             "then exit (no tmux, no network)")
    args = parser.parse_args()

    # Set TERMINAL_HOME BEFORE importing terminal_manager: it reads the env var
    # at import time to fix where new sessions are created. The flag wins, then
    # the folder the user picked during setup; with neither (a fresh install,
    # about to run setup) fall back to the home directory rather than to
    # whatever folder the app happens to be installed in.
    home = (os.path.abspath(os.path.expanduser(args.home)) if args.home
            else setup.configured_home())
    os.environ["TERMINAL_HOME"] = home or os.path.expanduser("~")

    if args.selftest:
        sys.exit(_run_selftest())
    if args.selftest_multi:
        sys.exit(_run_multi_selftest())
    if args.selftest_setup:
        sys.exit(_run_setup_selftest())
    if args.selftest_handover:
        _import_manager()
        sys.exit(_run_handover_selftest())

    # Resolve the optional VPS proxy (flags > env > the config's vps block).
    base, _tok = _load_vps_config(args.vps_url, args.vps_token)
    if _vps_enabled():
        print(f"Aggregating VPS sessions from {base}")

    _import_manager()
    _start_vps_poller()

    if setup.is_complete():
        print(f"Working directory: {os.environ['TERMINAL_HOME']}")
    else:
        print("First run: the app will ask for a Claude login and a folder.")

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
