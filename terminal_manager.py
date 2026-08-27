"""
Terminal Manager - tmux-backed web terminal for the AI-Hub dashboard.

Sessions live in a dedicated tmux server (socket: mlsterm), not as children of
this web server. The PTY here is only a *view* onto tmux: restarting, deploying
or crashing aihub-server tears down the view and leaves the shell - and anything
running in it - untouched. Reconnecting re-attaches and tmux redraws the screen.

The session registry is tmux itself (`tmux list-sessions`), so it survives a
restart of this process. Display names are stored as a tmux user option
(@webname) on each session for the same reason.

Worktree grouping rides the same mechanism: a session opened inside a worktree
carries @webworktree (the worktree id) and @webwtname (its display name), so
`tmux list-sessions` alone says which shells belong together. There is no second
datastore that could disagree with the shells that actually exist.

Persistence rests on the tmux server being owned by cron rather than by this
process: ensure-web-terminals.sh (@reboot + every minute) holds a _keepalive
session open, so the server sits in cron.service's cgroup where a
`systemctl restart aihub-server` - which only kills its own cgroup - cannot reach
it. KillMode=process on aihub-server.service achieves the same thing but needs
root; either is sufficient, neither is needed if the other is in place.

The gap that leaves: if the tmux server is ever down when a session is created,
tmux gets spawned from here and inherits this service's cgroup, so that session
dies on the next restart. The every-minute cron keeps that window under a minute.

Architecture:
  tmux server (independent lifetime)
    └── session web-<sid>  ← the shell; survives everything below
          ↑ attach
      tmux client on a PTY  ← disposable view, owned by this process
          ↑ read
      reader thread → scrollback ring buffer + broadcast queue
          ↑
      SSE stream generators (never touch the PTY fd directly)
"""

import base64
import fcntl
import json
import os
import pty
import queue
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid

import terminal_worktree


# ── tmux backend ──

TMUX_SOCKET = "mlsterm"       # dedicated server; isolated from the `farm` tmux
SESSION_PREFIX = "web-"
TMUX_HISTORY = "50000"        # lines of scrollback tmux keeps per session

# ── View storage (disposable; tmux is the source of truth) ──
views = {}                    # {session_id: TerminalView}
views_lock = threading.RLock()

SCROLLBACK_MAX = 32768        # bytes kept per view for instant replay on reconnect

# New sessions open here so `claude` runs in the project, not in $HOME. Without
# this, tmux new-session inherits the tmux server's cwd (/home/aihub) and you
# cannot launch Claude in the AI-Hub context. Override with TERMINAL_HOME.
START_DIR = os.environ.get("TERMINAL_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# tmux's ways of saying "definitively gone" vs. "I could not answer you". Only
# the former may be treated as proof of death; a timeout or a fork failure must
# never close a user's session.
_GONE_MARKERS = (
    "no server running",
    "no such file or directory",
    "error connecting to",
    "can't find session",
    "session not found",
)


def _resolve_tmux():
    """Absolute path to a usable tmux binary, or None if none is found.

    A double-clicked .app inherits launchd's minimal PATH (/usr/bin:/bin:...),
    which misses every place tmux normally lives - a Homebrew prefix or a
    userland (~/.local) build. shutil.which alone therefore returns None inside
    the bundled app even when tmux is installed, so we also probe the usual
    absolute locations. MTS_TMUX_BIN overrides everything for odd setups.
    """
    override = os.environ.get("MTS_TMUX_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    found = shutil.which("tmux")
    if found:
        return found
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".local", "bin", "tmux"),
                 "/opt/homebrew/bin/tmux",
                 "/usr/local/bin/tmux",
                 "/usr/bin/tmux"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# Resolved once at import. Falls back to the bare name so a tmux that only
# appears on PATH later still gets one last chance via the OS resolver.
TMUX_BIN = _resolve_tmux() or "tmux"


def _tmux(*args, timeout=10):
    """Run a tmux command against the dedicated socket.

    Never raises. A timed-out or un-spawnable tmux comes back as a non-zero
    result whose stderr matches no _GONE_MARKER, so callers can tell "tmux says
    no" apart from "tmux did not answer".
    """
    try:
        return subprocess.run(
            [TMUX_BIN, "-L", TMUX_SOCKET, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", "tmux timed out")
    except OSError as e:
        return subprocess.CompletedProcess(args, 1, "", f"tmux failed: {e}")


def _tmux_says_gone(result):
    """True only if tmux positively confirmed the thing does not exist."""
    err = (result.stderr or "").lower()
    return any(m in err for m in _GONE_MARKERS)


def _tmux_available():
    return _resolve_tmux() is not None


def _sname(sid):
    return f"{SESSION_PREFIX}{sid}"


def _session_exists(sid):
    """Does the tmux session exist?

    Assumes alive unless tmux confirms otherwise. A tmux hiccup (busy server,
    timeout) reporting "gone" would make the stream emit `exit`, which the client
    treats as final and never reconnects from - killing a live session over a
    transient blip.
    """
    r = _tmux("has-session", "-t", _sname(sid))
    if r.returncode == 0:
        return True
    return not _tmux_says_gone(r)


_last_good_sessions = []      # last listing tmux actually answered, for hiccups
_last_good_lock = threading.Lock()


def _list_sessions_raw():
    """(answered, sessions) straight from tmux.

    answered=False means tmux did not give us a usable answer, so the caller must
    not conclude anything - least of all that every session is gone.
    """
    fmt = "#{session_name}\t#{session_created}\t#{window_width}\t#{window_height}\t#{@webname}\t#{@webplan}\t#{@webloop}\t#{pane_current_command}\t#{pane_pid}\t#{@webstate}\t#{@webfile}\t#{session_activity}\t#{@webworktree}\t#{@webwtname}"
    r = _tmux("list-sessions", "-F", fmt)
    if r.returncode != 0:
        # A dead server genuinely means zero sessions; anything else is unknown.
        return _tmux_says_gone(r), []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[0].startswith(SESSION_PREFIX):
            continue
        sid = parts[0][len(SESSION_PREFIX):]
        try:
            created = float(parts[1])
        except ValueError:
            created = 0.0
        try:
            cols, rows = int(parts[2]), int(parts[3])
        except ValueError:
            cols, rows = 80, 24
        name = parts[4] if len(parts) > 4 and parts[4] else f"Terminal {sid[:4]}"
        plan = parts[5] if len(parts) > 5 else ""
        loop = parts[6] if len(parts) > 6 else ""
        cmd = parts[7] if len(parts) > 7 else ""
        pid = parts[8] if len(parts) > 8 else ""
        webstate = parts[9] if len(parts) > 9 else ""
        webfile = parts[10] if len(parts) > 10 else ""
        # tmux stamps session_activity whenever the session produces output. It
        # is what lets the status poll skip re-reading panes that cannot have
        # changed - see _pane_signals.
        activity = parts[11] if len(parts) > 11 else ""
        worktree = parts[12] if len(parts) > 12 else ""
        wtname = parts[13] if len(parts) > 13 else ""
        out.append({
            "id": sid, "name": name, "alive": True,
            "cols": cols, "rows": rows, "created_at": created, "plan": plan,
            "loop": loop, "cmd": cmd, "pid": pid, "webstate": webstate,
            "webfile": webfile, "activity": activity,
            "worktree": worktree, "worktree_name": wtname,
        })
    out.sort(key=lambda s: s["created_at"])
    with _last_good_lock:
        global _last_good_sessions
        _last_good_sessions = out
    return True, out


def _tmux_sessions():
    """Sessions tmux told us about; [] if it could not tell us."""
    return _list_sessions_raw()[1]


class TerminalView:
    """A tmux client on a PTY, broadcasting output to SSE subscribers.

    Disposable: if this dies the tmux session is unaffected, and the next
    reader re-attaches. Never kill the tmux session from here.
    """

    def __init__(self, sid):
        self.id = sid
        self.process = None
        self.master_fd = None
        self.client_tty = ""     # how tmux knows this client; see _open_view
        self.fd_lock = threading.RLock()   # guards master_fd against the close/read race
        self.scrollback = bytearray()
        self.scrollback_lock = threading.Lock()
        self.subscribers = []
        self.subscribers_lock = threading.Lock()
        self._reader_thread = None
        self._reader_done = threading.Event()
        self._closing = False

    @property
    def attached(self):
        return self.process is not None and self.process.poll() is None

    @property
    def alive(self):
        """Usable only if BOTH the tmux client and its reader thread are up.

        `attached` alone is not enough: if the reader thread dies (an unhandled
        exception, a closed fd) while the tmux client keeps running, the view
        looks healthy and produces nothing forever. Anything deciding whether to
        re-attach must ask this, not `attached`.
        """
        return (self.attached
                and not self._reader_done.is_set()
                and not self._closing)

    def subscribe(self):
        q = queue.Queue(maxsize=256)
        with self.subscribers_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.subscribers_lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def broadcast(self, event):
        """Push an event to all subscribers.

        A slow client (backgrounded tab, sleeping laptop) stops draining its
        queue. Previously that filled the queue and the subscriber was dropped
        silently and permanently - the shell kept running but the user never saw
        output again, which read as "my session was closed while I was away".
        Instead we drop the backlog and tell the client it desynced, so it can
        re-request a replay and carry on.
        """
        with self.subscribers_lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait({"type": "desync"})
                    except queue.Full:
                        pass


def _open_view(sid, cols=80, rows=24):
    """Attach a PTY-backed tmux client to an existing session."""
    view = TerminalView(sid)

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    # tmux identifies a client by the tty it attached on, and this is the only
    # moment that name is available - the slave is closed a few lines down.
    try:
        client_tty = os.ttyname(slave_fd)
    except OSError:
        client_tty = ""

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["LANG"] = "en_US.UTF-8"
    env.pop("TMUX", None)  # never let a nested client refuse to attach

    proc = subprocess.Popen(
        [TMUX_BIN, "-L", TMUX_SOCKET, "attach-session", "-t", _sname(sid)],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid, env=env,
        cwd=os.environ.get("HOME", "/"),
    )
    os.close(slave_fd)

    view.process = proc
    view.master_fd = master_fd
    view.client_tty = client_tty

    t = threading.Thread(target=_reader_loop, args=(view,), daemon=True)
    t.start()
    view._reader_thread = t
    return view


def _get_view(sid, cols=80, rows=24):
    """Return a live view for sid, re-attaching if the previous client died."""
    with views_lock:
        view = views.get(sid)
        if view is not None and view.alive:
            return view
        if view is not None:
            _close_view(view)
        if not _session_exists(sid):
            return None
        view = _open_view(sid, cols, rows)
        views[sid] = view
        return view


# ── Public API ──

def list_sessions():
    """Return all sessions, straight from tmux."""
    if not _tmux_available():
        return {"ok": False, "error": "tmux not installed", "sessions": []}
    answered, sessions = _list_sessions_raw()
    if not answered:
        # tmux did not answer (busy, timed out). Reaping here would close every
        # view over a blip; reporting [] would make the client hide the user's
        # terminals and helpfully open a brand new one. Serve the last answer we
        # trust and change nothing.
        with _last_good_lock:
            return {"ok": True, "sessions": list(_last_good_sessions), "stale": True}
    live = {s["id"] for s in sessions}
    # Take the dead views out of the map under the lock, then tear them down
    # outside it. _close_view terminates a tmux client and waits on it, and
    # holding views_lock across that stalls every keystroke, resize and stream
    # attach on the box for as long as the wait takes - a freeze that arrives
    # exactly when a session ends, which is the worst possible moment.
    with views_lock:
        dead = [views.pop(sid) for sid in [k for k in views if k not in live]]
    for view in dead:
        _close_view(view)
    return {"ok": True, "sessions": sessions}


# ── Activity classification for the dashboard status dots ──
# Read a session's visible tmux pane and infer what it is doing, so the browser
# can colour every session's dot from ONE poll without holding a live stream per
# session (which the browser's ~6-connections-per-host limit would cap at ~6):
#   working   = something is running (Claude shows "esc to interrupt")
#   attention = a prompt is waiting for you (permission box / yes-no question)
#   idle      = alive and waiting for input
#   offline   = the tmux session is gone
def _loop_deadline(raw):
    """Seconds-since-epoch this session's loop is armed until, or 0.

    @webloop carries a DEADLINE rather than a bare on/off flag so the state can
    heal itself: a loop that dies, crashes, or is killed stops refreshing the
    stamp and the session goes honestly idle once it lapses. An on/off flag
    would strand the session yellow forever - lying in the other direction.
    """
    if not raw:
        return 0.0
    try:
        return float(str(raw).split(":", 1)[0])
    except (TypeError, ValueError):
        return 0.0


# Commands that mean "nothing is running here". A shell at its prompt is idle;
# Claude sitting at its input box is idle too, and only its own UI text (below)
# can tell its idle apart from its working. Every OTHER foreground command -
# npm, pytest, git, curl, a build - IS work, and the pane text alone cannot see
# it: a quiet build looks exactly like a quiet prompt.
#
# 'node' is listed as quiet on purpose. Some Claude installs show up as node, so
# excluding it would strand those sessions permanently yellow - a dot that lies
# every second is worse than one that misses a bare `node script.js`.
_QUIET_CMDS = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tmux",
               "claude", "node"}

# A background job (`cmd &`) or a command Claude launched with run_in_background
# never changes pane_current_command and prints no "esc to interrupt", so the
# text + foreground-command checks both miss it and the dot stays green while
# real work runs. To catch it we look at the pane's process SUBTREE instead.
#
# Idle spine = the login shell + a single childless `claude`/`node` under it
# (empirically an idle Claude session is exactly `bash -> claude` with claude a
# leaf). Anything running below that spine is live work -> yellow.
_SPINE_SHELLS = {"bash", "zsh", "sh", "fish", "dash", "ksh",
                 "-bash", "-zsh", "-sh", "tmux", "login"}
# Ambiguous runtimes: a childless one is idle scaffolding (an idle Claude often
# shows as `node`); one that has spawned children is doing work. Kept in step
# with the `node` reasoning in _QUIET_CMDS - miss a bare `node x.js`, never lie.
_AMBIGUOUS_RUNTIME = {"claude", "node"}

# Claude Code renames its own process to its version, so on macOS tmux reports
# the pane's foreground command as e.g. "2.1.199" instead of "claude". A bare
# version string is therefore treated like the ambiguous claude/node runtime:
# idle at its prompt unless its process subtree is actually doing work. Without
# this an idle Claude session shows a version cmd that is not in _QUIET_CMDS and
# gets wrongly painted yellow (working) forever.
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)+$")


def _looks_like_version(cmd):
    return bool(cmd and _VERSION_RE.match(cmd.strip()))


# How much of the bottom of a pane counts as "what this session is doing or
# asking right now". A permission box, a y/n, and Claude's "esc to interrupt"
# spinner all sit within a few lines of the input; anything higher up is
# transcript - already dealt with, or merely text that happens to contain the
# words. Sized to cover a multi-line permission box plus the input and status
# rows beneath it.
#
# This matters more on a Mac than on the box: the Claude Code hooks that stamp
# @webstate are not installed here, so for local sessions the pane text is the
# ONLY signal. Scanning the whole screen meant any transcript containing "(y/n)"
# - documentation, a code block, an old answered prompt - painted the session
# red until something scrolled it away.
_PROMPT_TAIL_LINES = 20


def _pane_tail(low):
    """The live region of a captured pane: its last few non-blank-padded lines."""
    return "\n".join(low.splitlines()[-_PROMPT_TAIL_LINES:])


# A pick-one box: the selector caret sitting on a numbered option. Every dialog
# that stops and waits for you draws this same shape - a tool permission, a plan
# waiting for its go-ahead, a multiple-choice question - and they word themselves
# differently enough that matching their prose one phrase at a time has always
# missed some. A plan approval says "Would you like to proceed?", which nothing
# here was looking for, so a session sat green while it waited on you.
#
# The caret alone would not do: it also draws the input line you type into. With
# a number after it, it is a list, and a list in the live region is one nobody
# has answered.
_CHOICE_RE = re.compile(r"^\s*\u276f\s*\d+[.)]\s+\S", re.M)


def _build_proc_tree():
    """One ps snapshot -> {ppid: [(pid, comm), ...]} for the whole box.

    Built once per all_states() poll so classifying N sessions is one cheap call,
    not N. Zombies are dropped so a finished-but-unreaped child never strands a
    session yellow. Returns {} on any failure (callers then skip the subtree
    check and fall back to the text/foreground heuristics).
    """
    kids = {}
    try:
        out = subprocess.run(["ps", "-eo", "ppid=,pid=,stat=,comm="],
                             capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return kids
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        ppid, pid, stat, comm = parts
        if "Z" in stat:            # skip zombies / <defunct>
            continue
        kids.setdefault(ppid, []).append((pid, comm))
    return kids


def _subtree_has_work(pane_pid, kids):
    """True if the pane's process subtree holds a live command beyond the idle
    spine (login shell + a lone idle Claude). Catches foreground builds AND
    background jobs / Claude run_in_background work that the text check cannot."""
    if not pane_pid or not kids:
        return False
    seen = set()
    stack = list(kids.get(pane_pid, []))
    while stack:
        pid, comm = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        c = (comm or "").lower()
        if c in _AMBIGUOUS_RUNTIME:
            # Idle Claude/node is a childless leaf; children => real work.
            if kids.get(pid):
                return True
            continue
        if c in _SPINE_SHELLS:
            # A nested shell only counts if something runs under it.
            stack.extend(kids.get(pid, []))
            continue
        # Any other live process in the subtree is a running command.
        return True
    return False


# A `working` @webstate stamp carries the epoch it was written at (kind:ts). A
# genuinely busy Claude re-stamps it on every hook (PreToolUse/PostToolUse) and
# also shows "esc to interrupt"/agent rows in the pane, so a stamp that has gone
# quiet for longer than this is stale - a turn that was interrupted (Esc/Ctrl-C)
# or crashed before its Stop hook could reset it to idle. Ageing it out stops
# such sessions being stranded yellow forever.
_WEBSTATE_WORKING_TTL = 90.0


def _parse_webstate(raw):
    """('kind', epoch). New hook writes 'kind:epoch'; a bare legacy value gets
    epoch 0.0 so it reads as stale (heals sessions the old hook left 'working')."""
    if not raw:
        return "", 0.0
    kind, _, ts = raw.partition(":")
    try:
        return kind, float(ts)
    except ValueError:
        return kind, 0.0


# Reading a pane costs a `tmux capture-pane` subprocess, and the status poll runs
# every few seconds for EVERY session: fourteen sessions was fourteen processes
# spawned every three seconds, all day, on top of one `ps` over the whole
# machine. Most of those reads answer a question that cannot have changed - a
# session that has produced no output since the last poll shows the same screen.
#
# tmux already tracks that as session_activity, so the pane text is only re-read
# when it moves. The cheap signals (foreground command, process subtree, hook
# stamp) are still evaluated on every poll, so work that starts silently is
# still caught within one poll.
_pane_cache = {}                 # sid -> (activity, (interrupt, prompt))
_pane_cache_lock = threading.Lock()


def _pane_signals(sid, activity):
    """(esc_to_interrupt, prompt_waiting) for a session, re-reading the pane only
    when tmux says the session has produced output since we last looked.
    Returns None when tmux says the session is gone."""
    key = str(activity or "")
    if key:
        with _pane_cache_lock:
            hit = _pane_cache.get(sid)
        if hit and hit[0] == key:
            return hit[1]

    r = _tmux("capture-pane", "-p", "-t", _sname(sid))
    if r.returncode != 0:
        return None if _tmux_says_gone(r) else (False, False)
    low = (r.stdout or "").lower()
    tail = _pane_tail(low)
    signals = (
        "esc to interrupt" in tail,
        ("do you want to" in tail
         or "would you like to proceed" in tail            # a plan awaiting your go-ahead
         or "no, and tell claude what to do differently" in tail  # every permission box
         or bool(_CHOICE_RE.search(tail))
         or "y/n" in tail or "(y/n)" in tail or "[y/n]" in tail
         or "press enter to continue" in tail),
    )
    if key:
        with _pane_cache_lock:
            _pane_cache[sid] = (key, signals)
    return signals


def _forget_pane_cache(live_ids):
    with _pane_cache_lock:
        for sid in [k for k in _pane_cache if k not in live_ids]:
            _pane_cache.pop(sid, None)


def classify_session(sid, loop_raw="", cmd="", pane_pid="", proc_kids=None,
                     webstate="", activity=""):
    # webstate is the precise state stamped by Claude Code hooks (webterm-state.sh)
    # on the tmux session: attention (Notification/PermissionRequest), working
    # (PreToolUse/prompt), idle (Stop). It is preferred where it is reliable, but
    # the pane-text + process heuristics remain the floor so a session whose Claude
    # died mid-turn (never firing Stop) cannot be stranded by a stale stamp.
    # What a session is asking, or busy with, is at the BOTTOM of the pane -
    # that is where a terminal puts what is happening now. Matching the whole
    # visible screen meant an ALREADY ANSWERED permission box, or any transcript
    # text containing "(y/n)", kept the dot red long after the session had moved
    # on: you click it and it wants nothing. Only the live region counts.
    signals = _pane_signals(sid, activity)
    if signals is None:
        return "offline"
    interrupt, prompt_waiting = signals
    ws, ws_ts = _parse_webstate((webstate or "").strip().lower())
    if interrupt:
        return "working"
    # Needs you: a permission / y-n prompt in the live region, OR the hook caught
    # a Notification / PermissionRequest (more reliable than scraping pane text).
    if ws == "attention" or prompt_waiting:
        return "attention"
    # Background agents deliberately do NOT force yellow. The dot tracks the MAIN
    # conversation loop - "does this session need YOU?" - not whether every side
    # task has finished. A working main loop shows "esc to interrupt" (caught
    # above) and the hook stamps `working` (caught below via the fresh-stamp
    # branch); a main loop genuinely BLOCKED waiting on its agents keeps that fresh
    # `working` stamp too, so it stays yellow. But the instant the main loop
    # returns to the prompt, "esc to interrupt" disappears and Stop stamps `idle` -
    # even while a dispatched agent keeps churning in the rail ("↓ to manage" /
    # "◯ agent … ↓ 105k tokens"). Treating that lingering agent row as work is
    # exactly what pinned done-but-waiting sessions yellow for hours; it now reads
    # as idle (green) = your move. If you care about the background agent, the pane
    # still shows it; the dot just no longer lies that the session is busy.
    # A foreground command that is not a shell and not Claude is running work.
    # Ranked BELOW attention so a prompting installer (cmd=apt, asking y/n)
    # still shows red rather than being painted over yellow.
    if cmd and cmd.lower() not in _QUIET_CMDS and not _looks_like_version(cmd):
        return "working"
    # Background work the checks above cannot see: a `cmd &` job, or a command
    # Claude launched with run_in_background. It never changes the foreground
    # command and prints no spinner, so we inspect the process subtree. Ranked
    # BELOW attention so a y/n prompt still shows red, not yellow.
    if _subtree_has_work(pane_pid, proc_kids):
        return "working"
    # The hook stamped working (PreToolUse / prompt) - trust it only while Claude
    # is still the foreground process, so a dead session that never fired Stop
    # cannot stay stranded yellow.
    if (ws == "working" and (cmd.lower() in ("claude", "node") or _looks_like_version(cmd))
            and time.time() - ws_ts < _WEBSTATE_WORKING_TTL):
        return "working"
    # An armed loop is between iterations, not finished. The pane is quiet, so
    # the checks above see 'idle' and the dot would go green - which reads as
    # "done, waiting for you" when the session is going to carry on by itself.
    # Ranked BELOW attention on purpose: a loop must never mask a red.
    if time.time() < _loop_deadline(loop_raw):
        return "looping"
    return "idle"


def all_states():
    """{sid: status} for every live session - one cheap call drives all dots."""
    if not _tmux_available():
        return {"ok": True, "states": {}}
    answered, sessions = _list_sessions_raw()
    if not answered:
        with _last_good_lock:
            sessions = list(_last_good_sessions)
    kids = _build_proc_tree()
    _forget_pane_cache({s["id"] for s in sessions})
    return {"ok": True,
            "states": {s["id"]: classify_session(s["id"], s.get("loop", ""),
                                                 s.get("cmd", ""),
                                                 s.get("pid", ""), kids,
                                                 s.get("webstate", ""),
                                                 s.get("activity", ""))
                       for s in sessions},
            "plans": {s["id"]: s.get("plan", "") for s in sessions},
            "files": {s["id"]: s.get("webfile", "") for s in sessions},
            "loops": {s["id"]: s.get("loop", "") for s in sessions}}


# ── File uploads (browser -> session) ───────────────────────────────────────
# The dashboard runs on the VPS, so an image on the user's machine cannot reach a
# session by a normal drag/paste. The browser uploads the bytes here; we drop them
# into <session cwd>/.uploads and return the absolute path, which the frontend
# types into the prompt so Claude can read the file.
_UPLOAD_MAX = 5 * 1024 * 1024   # 5 MB - matches Claude Code's per-image limit
_UPLOAD_MIME = {
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
    "application/pdf", "text/plain", "text/csv", "text/markdown", "application/json",
}
_UPLOAD_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "application/pdf": ".pdf",
    "text/plain": ".txt", "text/csv": ".csv", "text/markdown": ".md",
    "application/json": ".json",
}


def session_cwd(sid):
    """The session's current working directory (tmux pane_current_path)."""
    r = _tmux("display-message", "-p", "-t", _sname(sid), "#{pane_current_path}")
    p = (r.stdout or "").strip()
    return p if p and os.path.isdir(p) else None


def save_upload(sid, name, mime, b64_data):
    """Decode a base64 upload from the browser into the session's .uploads dir.

    Validated (size + mime allowlist) and sanitised (basename only, safe chars),
    so a crafted name can never escape the uploads directory."""
    if not _session_exists(sid):
        return {"ok": False, "error": "no such session"}
    mime = (mime or "").split(";")[0].strip().lower()
    if mime and mime not in _UPLOAD_MIME:
        return {"ok": False, "error": "file type not allowed: %s" % mime}
    try:
        raw = base64.b64decode(b64_data or "")
    except (ValueError, TypeError):
        return {"ok": False, "error": "bad file data"}
    if not raw:
        return {"ok": False, "error": "empty file"}
    if len(raw) > _UPLOAD_MAX:
        return {"ok": False, "error": "file too large (max 5MB)"}
    cwd = session_cwd(sid) or START_DIR
    updir = os.path.join(cwd, ".uploads")
    base = os.path.basename(name or "upload")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "upload"
    if len(base) > 80:
        base = base[-80:]
    if not os.path.splitext(base)[1]:
        base += _UPLOAD_EXT.get(mime, "")
    fname = "%d-%s" % (int(time.time()), base)
    try:
        os.makedirs(updir, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": "mkdir failed: %s" % e}
    fpath = os.path.join(updir, fname)
    if not os.path.realpath(fpath).startswith(os.path.realpath(updir) + os.sep):
        return {"ok": False, "error": "unsafe path"}
    try:
        with open(fpath, "wb") as f:
            f.write(raw)
        os.chmod(fpath, 0o644)
    except OSError as e:
        return {"ok": False, "error": "write failed: %s" % e}
    return {"ok": True, "path": fpath, "name": fname, "bytes": len(raw)}


def session_diff(sid):
    """Read-only git status + diff for a session's working directory.

    Never mutates anything - just runs status/diff so you can review what a
    session changed. The diff is capped so a huge change set can't balloon the
    response."""
    if not _session_exists(sid):
        return {"ok": False, "error": "no such session"}
    cwd = session_cwd(sid)
    if not cwd:
        return {"ok": False, "error": "no working directory"}

    def _git(*args, timeout=12):
        try:
            return subprocess.run(["git", "-C", cwd, *args],
                                  capture_output=True, text=True, timeout=timeout).stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    top = ""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            top = r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    if not top:
        return {"ok": True, "cwd": cwd, "repo": None, "text": "(%s is not a git repository)" % cwd}

    status = _git("status", "-sb")
    stat = _git("diff", "--stat")
    full = _git("diff", timeout=15)
    MAXD = 200 * 1024
    trunc = ""
    if len(full) > MAXD:
        full = full[:MAXD]
        trunc = "\n\n[... diff truncated at 200 KB - run `git diff` in the session for the rest ...]"
    text = ("# %s\n\n$ git status -sb\n%s\n$ git diff --stat\n%s\n$ git diff\n%s%s"
            % (top, status, stat, full, trunc))
    return {"ok": True, "cwd": cwd, "repo": top, "text": text}


_TRUST_MARKERS = ("do you trust the files in this folder",
                  "yes, i trust this folder")
# What a Claude that is up and waiting for you looks like. The input caret is the
# signal: the footer text varies with version and settings - one session says
# "auto mode on", another only "for agents" - but the caret is what "ready" means
# on screen, and it is the same in every version.
_READY_CARET = "❯"
_READY_MARKERS = ("auto mode on", "for shortcuts", "bypass permissions")


def _looks_ready(text):
    if any(line.strip().startswith(_READY_CARET) for line in text.splitlines()):
        return True
    low = text.lower()
    return any(m in low for m in _READY_MARKERS)


def worktree_brief(path, branch, repo, base, count):
    """The one thing a fresh Claude in a worktree cannot work out for itself.

    It already knows the repo and the branch - it starts in the folder and reads
    the repo's CLAUDE.md. What it cannot see is that this checkout is disposable,
    which tree it must NOT wander into, that other terminals are editing the same
    files, and where the work is supposed to end up.

    One line, because a newline would submit it half-written.
    """
    others = ("%d terminals share this folder, so check git status before "
              "editing widely. " % count) if count > 1 else ""
    return (
        "Context before we start: you are in a git worktree at %s, on branch %s, "
        "cut from %s of the repo at %s. This checkout is disposable - the main "
        "checkout at %s is untouched, so do not switch branches here and do not "
        "edit anything outside this folder. %s"
        "When the work is done the route is: commit here, push this branch, open "
        "a PR - and ask me before you push or deploy anything. "
        "Reply with a single short line that you are ready, then wait."
        % (path, branch, base or "its base", repo, repo, others))


def _prime_claude(sid, brief=None, window=90.0):
    """Get Claude past its opening prompt and, optionally, hand it the brief.

    Two things happen on a first launch in a brand new directory. Claude asks
    whether you trust the folder - always, because a worktree is a folder it has
    never seen - and until that is answered, "autostart" has delivered a session
    that is not actually started. Then it needs a moment before it can take input.

    Deliberately narrow on both counts: the trust prompt is answered only when
    THAT prompt is on screen, and the brief is typed only once Claude looks
    ready. Nothing is blind-fired into a terminal that might be showing something
    else entirely. If neither state ever appears, nothing is sent.
    """
    def watch():
        deadline = time.time() + window
        trusted = False
        while time.time() < deadline:
            time.sleep(0.7)
            r = _tmux("capture-pane", "-p", "-t", _sname(sid))
            if r.returncode != 0:
                if _tmux_says_gone(r):
                    return
                continue
            low = (r.stdout or "").lower()
            if not trusted and any(m in low for m in _TRUST_MARKERS):
                _tmux("send-keys", "-t", _sname(sid), "Enter")
                trusted = True
                continue
            if not brief:
                if trusted:
                    return          # nothing else to do
                continue
            if _looks_ready(r.stdout or "") and not any(m in low for m in _TRUST_MARKERS):
                # Let it finish painting before typing into it - the caret shows
                # up a moment before the input is actually listening.
                time.sleep(1.5)
                # -l sends the text literally, so a word like "Enter" inside the
                # brief stays a word instead of becoming a keypress.
                _tmux("send-keys", "-t", _sname(sid), "-l", brief)
                time.sleep(0.4)
                _tmux("send-keys", "-t", _sname(sid), "Enter")
                return
    threading.Thread(target=watch, daemon=True, name="wt-prime-%s" % sid[:6]).start()


def start_session(name=None, cwd=None, worktree=None, worktree_name=None,
                  autostart=False, brief=None):
    """Create a new tmux-backed session.

    `cwd` opens it somewhere other than the configured working folder - used by
    a handover, which has to land in the folder that matches the machine the
    work came from, not wherever new sessions normally start, and by a worktree
    group, whose shells all open inside its checkout.

    `worktree`/`worktree_name` stamp @webworktree/@webwtname, which is the whole
    of the grouping mechanism: list_sessions() reads them straight back out.

    `autostart` types `claude` into the fresh shell. Off by default - a new
    terminal should be a terminal, and starting an agent is a decision rather
    than a side effect of opening a window.
    """
    if not _tmux_available():
        return {"ok": False, "error": "tmux not installed"}

    sid = uuid.uuid4().hex[:12]
    if name is None:
        # Default sessions are named after the folder they open in: for
        # ~/code/Ledger the first is "Ledger", then "Ledger 2", "Ledger 3"...
        # (the bare name counts as 1). Read from START_DIR rather than fixed at
        # import, because the folder can be changed from the settings panel.
        base = os.path.basename(START_DIR.rstrip(os.sep)) or "Session"
        existing = []
        for s in _tmux_sessions():
            nm = s["name"]
            if nm == base:
                existing.append(1)
            elif nm.startswith(base + " "):
                try:
                    existing.append(int(nm.split(" ")[-1]))
                except (ValueError, IndexError):
                    pass
        n = 1
        while n in existing:
            n += 1
        name = base if n == 1 else f"{base} {n}"

    start_dir = START_DIR
    if cwd and os.path.isdir(os.path.expanduser(cwd)):
        start_dir = os.path.expanduser(cwd)

    cols, rows = 80, 24
    r = _tmux("new-session", "-d", "-s", _sname(sid), "-c", start_dir,
              "-x", str(cols), "-y", str(rows), "/bin/bash", "--login")
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "tmux new-session failed").strip()}

    _tmux("set-option", "-t", _sname(sid), "status", "off")
    _tmux("set-option", "-t", _sname(sid), "history-limit", TMUX_HISTORY)
    _tmux("set-option", "-t", _sname(sid), "destroy-unattached", "off")
    _tmux("set-option", "-t", _sname(sid), "@webname", name)
    if worktree:
        _tmux("set-option", "-t", _sname(sid), "@webworktree", worktree)
        _tmux("set-option", "-t", _sname(sid), "@webwtname", worktree_name or worktree)

    if autostart:
        _tmux("send-keys", "-t", _sname(sid), "claude", "Enter")
        _prime_claude(sid, brief)

    return {"ok": True, "session": {
        "id": sid, "name": name, "alive": True,
        "cols": cols, "rows": rows, "created_at": time.time(),
        "worktree": worktree or "", "worktree_name": worktree_name or "",
    }}


def stop_session(sid):
    """Explicitly kill a session. The only path that destroys a shell."""
    if not _session_exists(sid):
        return {"ok": False, "error": "session not found"}
    with views_lock:
        view = views.pop(sid, None)
    if view:
        _close_view(view)
    _tmux("kill-session", "-t", _sname(sid))
    return {"ok": True}


def view_stats():
    """What this process is actually holding open.

    A terminal app that goes sticky after hours of use is nearly always holding
    something it should have let go of - a view whose tmux client died, a
    subscriber queue nobody drains, a reader thread that outlived its fd. None of
    that is visible from the outside, which is what makes such a bug a matter of
    opinion. This makes it a number.
    """
    with views_lock:
        views_now = list(views.values())
    subs = 0
    dead = 0
    for v in views_now:
        with v.subscribers_lock:
            subs += len(v.subscribers)
        if not v.alive:
            dead += 1
    return {
        "views": len(views_now),
        "dead_views": dead,
        "subscribers": subs,
        "threads": threading.active_count(),
        "pane_cache": len(_pane_cache),
        "size_reports": sum(len(v) for v in _size_reports.values()),
    }


def stop_all():
    """Detach all views on shutdown. Deliberately leaves tmux sessions running.

    This is what makes a deploy survivable: the web server goes away, the shells
    do not.
    """
    with views_lock:
        all_views = list(views.values())
        views.clear()
    for view in all_views:
        _close_view(view)


def rename_session(sid, name):
    if not _session_exists(sid):
        return {"ok": False, "error": "session not found"}
    _tmux("set-option", "-t", _sname(sid), "@webname", name)
    for s in _tmux_sessions():
        if s["id"] == sid:
            return {"ok": True, "session": s}
    return {"ok": True, "session": {"id": sid, "name": name, "alive": True}}


# ── Worktree groups ──
# One git worktree, several shells whose cwd is inside it, drawn as a folder in
# the sidebar. terminal_worktree owns the git half; this owns the tmux half and
# the join between them, which is nothing more than the @webworktree stamp.
#
# START_DIR is read at CALL time, never captured: the folder the app is set to
# is changeable from the settings panel, and a worktree cut from the folder you
# used to have open would be a bad surprise.

def _sessions_in_worktree(wt_id):
    return [s for s in _tmux_sessions() if s.get("worktree") == wt_id]


def _group_display_name(wt_id, sessions=None):
    """The human name for a group, recovered from its own sessions.

    git knows an id and a branch, not a name, so the name lives on the sessions
    as @webwtname. Any one of them can answer.
    """
    for s in (sessions if sessions is not None else _sessions_in_worktree(wt_id)):
        if s.get("worktree_name"):
            return s["worktree_name"]
    return wt_id


def _next_group_index(sessions, display):
    """Number the next shell after the highest already in the group.

    Closing "Fix 2" of three and adding one back should give you "Fix 4", not a
    second "Fix 3" sitting next to the first.
    """
    highest = 0
    for s in sessions:
        m = re.match(r"^" + re.escape(display) + r" (\d+)$", s.get("name") or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return max(highest, len(sessions)) + 1


def list_repos():
    """Repos this machine can cut a worktree from, plus which one is the default.

    The folder the app is set to is always in the list even if it sits outside
    the folders that get scanned, because that is the one you would reach for
    first and its absence would read as a bug.
    """
    try:
        repos = terminal_worktree.discover_repos(always=[START_DIR])
    except (terminal_worktree.WorktreeError, subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": str(e), "repos": [], "default": ""}
    default = ""
    real_start = os.path.realpath(START_DIR)
    for r in repos:
        if os.path.realpath(r["path"]) == real_start:
            default = r["path"]
    if not default and terminal_worktree.is_git_repo(START_DIR):
        # Set to a repo the scan did not reach. Offer it anyway - it is the one
        # every session already opens in.
        repos.insert(0, {"path": START_DIR,
                         "name": os.path.basename(START_DIR.rstrip(os.sep)),
                         "slug": terminal_worktree.remote_slug(START_DIR),
                         "branch": terminal_worktree.current_branch(START_DIR),
                         "worktrees": 0})
        default = START_DIR
    return {"ok": True, "repos": repos, "default": default or (repos[0]["path"] if repos else "")}


def clone_repo(full_name, dest_parent=None, name=None):
    """Clone a GitHub repo so it can be worktreed. Returns the new checkout."""
    parent = dest_parent or os.path.dirname(START_DIR.rstrip(os.sep)) or os.path.expanduser("~")
    try:
        path = terminal_worktree.clone(full_name, parent, name)
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git clone timed out"}
    return {"ok": True, "repo": {
        "path": path, "name": os.path.basename(path.rstrip(os.sep)),
        "slug": terminal_worktree.remote_slug(path),
        "branch": terminal_worktree.current_branch(path), "worktrees": 0}}


def github_repos():
    try:
        return {"ok": True, "repos": terminal_worktree.github_repos()}
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e), "repos": []}


def create_worktree_group(name, base=None, count=1, autostart=False, repo=None):
    """One worktree plus `count` shells already inside it.

    A create that produces no shells at all disposes of the worktree again
    rather than leaving an empty directory behind: at that point nothing has
    happened in it, so there is nothing to preserve.
    """
    try:
        count = int(count)
    except (TypeError, ValueError):
        return {"ok": False, "error": "count must be a number"}
    if count < 1 or count > terminal_worktree.MAX_SESSIONS:
        return {"ok": False, "error": "count must be between 1 and %d"
                                      % terminal_worktree.MAX_SESSIONS}
    if not _tmux_available():
        return {"ok": False, "error": "tmux not installed"}

    # An explicit repo wins; otherwise the folder the app is set to. Checked
    # against the pick list rather than taken on trust, so an id from a stale
    # page cannot aim a checkout at an arbitrary path on this machine.
    repo = os.path.expanduser(repo) if repo else START_DIR
    if os.path.realpath(repo) != os.path.realpath(START_DIR):
        known = {os.path.realpath(r["path"]) for r in list_repos().get("repos", [])}
        if os.path.realpath(repo) not in known:
            return {"ok": False, "error": "unknown repo: %s" % repo}
    try:
        info = terminal_worktree.create(
            name, base or terminal_worktree.current_branch(repo) or "HEAD", repo)
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git worktree add timed out"}

    brief = worktree_brief(info["path"], info["branch"], repo, info["base"], count) \
        if autostart else None

    sessions, errors = [], []
    for i in range(1, count + 1):
        r = start_session(name="%s %d" % (info["name"], i), cwd=info["path"],
                          worktree=info["id"], worktree_name=info["name"],
                          autostart=autostart, brief=brief)
        if r.get("ok"):
            sessions.append(r["session"])
        else:
            errors.append(r.get("error") or "session failed to start")

    if not sessions:
        try:
            terminal_worktree.dispose(info["id"], repo, force=True)
        except Exception:
            pass
        return {"ok": False, "error": "; ".join(errors) or "no sessions started"}

    return {"ok": True, "worktree": {
        "id": info["id"], "name": info["name"], "branch": info["branch"],
        "path": info["path"], "base": info["base"], "seeded": info["seeded"],
        "repo": repo, "repo_name": os.path.basename(repo.rstrip(os.sep)),
    }, "sessions": sessions, "errors": errors}


def add_session_to_worktree(wt_id, name=None, autostart=False):
    """One more shell in an existing worktree."""
    if not terminal_worktree.exists(wt_id):
        return {"ok": False, "error": "worktree not found"}
    existing = _sessions_in_worktree(wt_id)
    if len(existing) >= terminal_worktree.MAX_SESSIONS:
        return {"ok": False, "error": "a worktree holds at most %d sessions"
                                      % terminal_worktree.MAX_SESSIONS}
    display = _group_display_name(wt_id, existing)
    label = name or "%s %d" % (display, _next_group_index(existing, display))
    path = terminal_worktree.worktree_path(wt_id)
    owner = terminal_worktree.owner_repo(wt_id) or START_DIR
    brief = worktree_brief(path, terminal_worktree.branch_name(wt_id), owner,
                           terminal_worktree.default_branch(owner),
                           len(existing) + 1) if autostart else None
    return start_session(name=label, cwd=path,
                         worktree=wt_id, worktree_name=display,
                         autostart=autostart, brief=brief)


def remove_worktree_group(wt_id, force=False, keep_branch=True):
    """Close every shell in a worktree, then remove the checkout.

    The dirty check runs BEFORE anything is killed, so a refusal costs you
    nothing: the shells are still open and the changes are still there.
    """
    try:
        terminal_worktree.validate_id(wt_id)
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e)}

    sessions = _sessions_in_worktree(wt_id)

    if not force and terminal_worktree.exists(wt_id):
        try:
            if terminal_worktree.is_dirty(wt_id):
                return {"ok": False, "dirty": True, "error":
                        "This worktree has uncommitted changes. Removing it "
                        "throws them away."}
        except (terminal_worktree.WorktreeError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": str(e)}

    for s in sessions:
        stop_session(s["id"])

    try:
        # No repo passed: the checkout names its own owner, which is the only
        # answer that stays right when several repos are in play.
        terminal_worktree.dispose(wt_id, keep_branch=keep_branch, force=True)
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e), "closed": len(sessions)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git worktree remove timed out",
                "closed": len(sessions)}

    return {"ok": True, "closed": len(sessions),
            "branch": terminal_worktree.branch_name(wt_id) if keep_branch else ""}


def worktree_status(wt_id):
    """What a group holds that you would lose. On demand only - see the module."""
    try:
        terminal_worktree.validate_id(wt_id)
        return dict(terminal_worktree.status(wt_id), ok=True)
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git took too long"}


def finish_worktree(wt_id, message=None, open_pr=True):
    """Commit, push, open a PR. Reaches off this machine, so only when asked."""
    try:
        terminal_worktree.validate_id(wt_id)
        return dict(terminal_worktree.finish(wt_id, message, open_pr), ok=True)
    except terminal_worktree.WorktreeError as e:
        return {"ok": False, "error": str(e)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git took too long"}


def stale_worktree_branches():
    """Empty wt/* branches, per repo, left behind by removed worktrees."""
    repos = list_repos().get("repos", [])
    out = []
    for r in repos:
        try:
            rows = terminal_worktree.stale_branches(r["path"])
        except (terminal_worktree.WorktreeError, subprocess.TimeoutExpired, OSError):
            continue
        if rows:
            out.append({"repo": r["path"], "name": r["name"],
                        "branches": [b["name"] for b in rows]})
    return {"ok": True, "repos": out,
            "total": sum(len(r["branches"]) for r in out)}


def delete_worktree_branches(repo, names):
    known = {r["path"] for r in list_repos().get("repos", [])}
    if repo not in known:
        return {"ok": False, "error": "unknown repo"}
    try:
        return dict(terminal_worktree.delete_branches(repo, names), ok=True)
    except (terminal_worktree.WorktreeError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}


def list_worktree_groups(with_dirty=False):
    """Every group the sidebar should draw, from git and tmux together.

    with_dirty is off by default and the poll leaves it off: `git status` on a
    large checkout, several times a minute, for an answer nothing is looking at,
    is how you make a laptop warm. The removal guard asks at the one moment it
    matters.

    A group whose directory has been removed by hand still appears, flagged
    `missing`, because its sessions are still open and still need somewhere to
    live in the list.
    """
    repo = START_DIR
    try:
        # Every repo's worktrees, not just the folder the app is set to: a group
        # you made from another repo must not vanish from the list the moment it
        # is not the current one.
        trees = terminal_worktree.list_worktrees(with_dirty=with_dirty)
    except (terminal_worktree.WorktreeError, subprocess.TimeoutExpired, OSError):
        trees = []

    by_id = {}
    for s in _tmux_sessions():
        if s.get("worktree"):
            by_id.setdefault(s["worktree"], []).append(s)

    groups, seen = [], set()
    for t in trees:
        members = by_id.get(t["id"], [])
        seen.add(t["id"])
        g = dict(t)
        g["name"] = _group_display_name(t["id"], members)
        g["sessions"] = [m["id"] for m in members]
        g["missing"] = False
        groups.append(g)

    for wt_id, members in by_id.items():
        if wt_id in seen:
            continue
        groups.append({
            "id": wt_id, "name": _group_display_name(wt_id, members),
            "branch": terminal_worktree.BRANCH_PREFIX + wt_id, "path": "",
            "sessions": [m["id"] for m in members], "missing": True,
        })

    groups.sort(key=lambda g: g["name"].lower())
    return {"ok": True, "worktrees": groups,
            "current_branch": terminal_worktree.current_branch(repo),
            "is_repo": terminal_worktree.is_git_repo(repo),
            "folder": repo,
            "max_sessions": terminal_worktree.MAX_SESSIONS}


def write_session(sid, data):
    """Write raw input to a session's PTY."""
    view = _get_view(sid)
    if not view:
        return {"ok": False, "error": "session not found"}
    with view.fd_lock:
        if view.master_fd is None or view._closing:
            return {"ok": False, "error": "session not running"}
        try:
            os.write(view.master_fd, data.encode("utf-8"))
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}


# A session has ONE tmux window shared by every browser viewing it, so it can
# only be one size. When the same session is open on a phone (44 cols) and a PC
# (116) at once, sizing to either clips or mis-renders the other. We instead size
# it to the SMALLEST live viewer: content then fits every screen (the bigger one
# just shows empty space on the right). Each browser reports its size on fit and
# on a ~1.5s heartbeat; a report older than SIZE_TTL is dropped, so when a viewer
# leaves the window grows back to whoever remains.
#
# Reports are keyed BY VIEWER so a viewer's new size replaces its own previous
# one. Keyed only by arrival, one viewer that shrinks and grows back inside the
# TTL competes with itself: min() keeps the width it no longer has, and since
# nothing re-evaluates until the next report arrives, the window stays clamped
# there indefinitely. That is a terminal stuck at half the window with output
# dropping off the right-hand edge, curable only by restarting the process that
# holds these dicts. A viewer that stops reporting still ages out, so the window
# still grows back when one leaves.
_size_reports = {}            # sid -> {viewer: (cols, rows, ts)}
_applied_size = {}            # sid -> (cols, rows) currently pushed to the view
_size_lock = threading.Lock()
SIZE_TTL = 6.0

def resize_session(sid, cols, rows, viewer=""):
    """Resize a session, sizing to the smallest live viewer (see note above)."""
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    now = time.time()
    with _size_lock:
        live = {v: e for v, e in (_size_reports.get(sid) or {}).items()
                if now - e[2] < SIZE_TTL}
        live[viewer or "anon"] = (cols, rows, now)
        _size_reports[sid] = live
        tcols = min(e[0] for e in live.values())
        trows = min(e[1] for e in live.values())
        if _applied_size.get(sid) == (tcols, trows):
            return {"ok": True, "cols": tcols, "rows": trows, "unchanged": True}

    view = _get_view(sid, tcols, trows)
    if not view:
        return {"ok": False, "error": "session not found"}
    with view.fd_lock:
        if view.master_fd is None or view._closing:
            return {"ok": False, "error": "session not running"}
        try:
            fcntl.ioctl(view.master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", trows, tcols, 0, 0))
            os.kill(view.process.pid, signal.SIGWINCH)
        except OSError as e:
            return {"ok": False, "error": str(e)}

    # Keep the tmux window itself in step, so a reattach keeps the same geometry.
    _tmux("resize-window", "-t", _sname(sid), "-x", str(tcols), "-y", str(trows))
    with _size_lock:
        _applied_size[sid] = (tcols, trows)
    return {"ok": True, "cols": tcols, "rows": trows}


def iter_session_events(sid):
    """Generator yielding a session's events as dicts.

    Split out from stream_session_output so the same event source can feed two
    very different endpoints: one session on its own connection, and N sessions
    merged onto one (see server.py's stream-multi, which the grid needs because
    a browser gives an origin about six connections and a wall of panes would
    spend the lot on output alone). Formatting lives with the endpoint; this
    yields the events themselves.

    Sends scrollback replay first, then live output. On reconnect after a server
    restart the view is re-attached here and tmux redraws the full screen.

    A view is disposable and can die under a perfectly healthy session (its tmux
    client exits, the PTY hits EIO, a reaper closes it). This generator therefore
    follows the *session*, not any one view: when the current view dies it
    re-attaches a fresh one and re-subscribes, rather than sitting on a dead
    view's queue. Getting that wrong is invisible to the client - pings still
    flow, so the browser sees a healthy stream and never reconnects, while
    keystrokes go on reaching the shell whose output nobody is listening for.
    That is a permanently frozen terminal with a blinking cursor.
    """
    if not _session_exists(sid):
        yield {"type": "error", "error": "session not found"}
        return

    ping_interval = 15
    last_ping = 0

    while True:
        view = _get_view(sid)
        if not view:
            # _get_view only fails to attach when tmux no longer has the session.
            yield {"type": "exit", "code": 0}
            return

        q = view.subscribe()
        try:
            # The screen, handed over AFTER subscribing, on EVERY attach.
            #
            # A fresh tmux client paints the whole screen the moment it attaches,
            # and _open_view starts its reader before this generator has a queue
            # to receive anything. Those bytes therefore reached the scrollback
            # and nobody else. Replaying BEFORE subscribing could not close that
            # gap either: whatever arrived between the snapshot and the subscribe
            # was broadcast to no one and then never replayed, so a re-attach
            # could leave a browser holding whatever the dying client last drew -
            # most visibly tmux's own "[terminated]" - with nothing arriving to
            # overwrite it. Reading the scrollback out AFTER subscribing covers
            # the whole timeline with no seam, because _reader_loop appends and
            # broadcasts under one lock: anything not in this snapshot is already
            # in the queue, and nothing is in both.
            #
            # Sent on every attach and even when empty. The outer loop re-attaches
            # under a live session, and that new client's screen is exactly what
            # the browser has not got; an empty one tells it to clear a pane whose
            # contents are now a lie.
            with view.scrollback_lock:
                snapshot = bytes(view.scrollback)
            yield {"type": "replay",
                   "data": base64.b64encode(snapshot).decode("ascii")}

            # Nudge tmux into redrawing so a fresh client sees the current screen
            # rather than waiting for the next keystroke. Targeted at the client's
            # tty, which is how tmux names a client: this used to pass the session
            # name, which refresh-client answers with "can't find client", so the
            # nudge had never once fired.
            if view.client_tty:
                _tmux("refresh-client", "-t", view.client_tty)

            while True:
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    if not view.alive:
                        break  # re-attach on the outer loop
                    last_ping += 1
                    if last_ping >= ping_interval:
                        yield {"type": "ping"}
                        last_ping = 0
                    # The shell is gone only if tmux says so - a dead view just
                    # means our client dropped, and reconnecting will re-attach.
                    if not _session_exists(sid):
                        yield {"type": "exit", "code": 0}
                        return
                    continue

                if event.get("type") == "detached":
                    break  # the view died; re-attach on the outer loop
                yield event
                last_ping = 0
                if event.get("type") == "exit":
                    return
        finally:
            view.unsubscribe(q)

        # Fell out of the inner loop: the view died under a live session. Loop
        # round to re-attach. The brief pause keeps a session that refuses to
        # attach from spinning this thread at full tilt.
        time.sleep(0.25)


def stream_session_output(sid):
    """One session's events as SSE text - the single-pane endpoint.

    Kept as its own entry point because a solo stream needs no sid on the wire:
    the connection *is* the session. The grid's merged stream tags every event
    instead; see server.py.
    """
    for event in iter_session_events(sid):
        yield f"data: {json.dumps(event)}\n\n"


# ── Internal helpers ──

# How far past the cut point we will look for a clean place to start. An escape
# sequence is a handful of bytes; anything longer than this is not one, and
# scanning further would throw away real output for nothing.
_TRIM_SCAN = 512


def _trim_scrollback(buf, limit):
    """Drop the oldest bytes down to `limit`, cutting only where a terminal can
    safely start reading.

    A blind `del buf[:n]` can slice an escape sequence in half. The tail then
    leads the buffer, and since this buffer is replayed verbatim to a fresh
    xterm on every reconnect, that terminal is handed the middle of a sequence:
    it prints the remainder as literal text ("5;180m" and friends) and its
    parser is left in the wrong state, so everything after it renders shifted
    and overlapping. That is the corrupted screen you get after a session has
    been running long enough to wrap this buffer - and only after a reconnect,
    which is what made it look random.

    So: cut at `limit`, then walk forward to the next ESC, which is always a
    valid place to begin. If there is no ESC nearby, the region is plain text
    and any byte that is not a UTF-8 continuation will do.
    """
    excess = len(buf) - limit
    if excess <= 0:
        return
    start = excess
    stop = min(len(buf), start + _TRIM_SCAN)
    esc = buf.find(b"\x1b", start, stop)
    if esc != -1:
        start = esc
    else:
        # No sequence in reach: just do not start inside a multi-byte character.
        while start < stop and (buf[start] & 0xC0) == 0x80:
            start += 1
    del buf[:start]


def _reader_loop(view):
    """One reader thread per view: PTY → scrollback + broadcast.

    Whatever happens, this must mark the view dead and say so on the way out:
    subscribers cannot see this thread, only its silence, and silence is
    indistinguishable from an idle shell.
    """
    try:
        while True:
            with view.fd_lock:
                fd = view.master_fd
                if fd is None or view._closing:
                    break
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except (ValueError, OSError):
                break
            if not ready:
                continue
            # Re-check under the lock: _close_view may have closed the fd while we
            # were in select(), and reading a closed/reused fd is a real bug.
            with view.fd_lock:
                if view.master_fd is None or view._closing:
                    break
                try:
                    data = os.read(view.master_fd, 4096)
                except (OSError, ValueError):
                    break
            if not data:
                break

            # Both under the one lock, so a subscriber taking a snapshot of the
            # scrollback can never straddle a chunk: whatever it does not hold is
            # queued for it instead, and nothing is delivered twice.
            with view.scrollback_lock:
                view.scrollback.extend(data)
                if len(view.scrollback) > SCROLLBACK_MAX:
                    _trim_scrollback(view.scrollback, SCROLLBACK_MAX)
                view.broadcast({"type": "output",
                                "data": base64.b64encode(data).decode("ascii")})
    finally:
        # Mark dead before announcing, so anyone who checks .alive on the way
        # past agrees with the message.
        view._reader_done.set()
        # The client detached. That says nothing about the shell, so only report
        # an exit if tmux confirms the session is really gone; otherwise tell
        # subscribers to re-attach rather than leaving them on a dead queue.
        if not _session_exists(view.id):
            view.broadcast({"type": "exit", "code": 0})
        else:
            view.broadcast({"type": "detached"})


def _close_view(view):
    """Tear down a view's client + fd. Never touches the tmux session."""
    with view.fd_lock:
        view._closing = True
        fd, view.master_fd = view.master_fd, None

    if view.process and view.process.poll() is None:
        view.process.terminate()
        try:
            # A tmux client that has not gone in a second is not going to; it is
            # detached from a session that no longer exists. Waiting five was
            # five seconds of a stalled close.
            view.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            view.process.kill()
            try:
                view.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass   # reaped by the OS; never block the caller on it

    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


# ── Backwards compatibility shims ──

def start_terminal():
    result = start_session()
    if result["ok"]:
        s = result["session"]
        return {"ok": True, "cols": s["cols"], "rows": s["rows"], "sid": s["id"]}
    return result

def stop_terminal():
    stop_all()
    return {"ok": True}

def is_terminal_alive():
    return bool(_tmux_sessions())

def stream_terminal_output():
    sessions = _tmux_sessions()
    if sessions:
        return stream_session_output(sessions[0]["id"])
    result = start_session()
    if result["ok"]:
        return stream_session_output(result["session"]["id"])
    return iter([f"data: {json.dumps({'type': 'error', 'error': 'could not start'})}\n\n"])
