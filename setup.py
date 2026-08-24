#!/usr/bin/env python3
"""First-run setup and per-user configuration for Mattdaemon.

The app used to be one person's: the working directory was hard-coded to
~/Documents/AI-Hub, and whatever Claude Code login happened to be on the machine
was simply used. That is fine for the machine it was written on and useless for
anyone else, so this module owns the two things a new user must supply before
the terminal is any use to them:

    1. a Claude Code login of their own (their subscription, not the author's), and
    2. the folder their sessions should open in.

Everything is per-user and lives outside the app bundle:

    ~/Library/Application Support/MLSuiteTerminal/mts-config.json   (macOS)
    ~/.config/mattdaemon/mts-config.json                            (elsewhere)

so the .app itself carries no configuration, no folder and no token, and can be
handed to someone else as-is. A mts-config.json sitting next to this file is
still read - that is the checkout/dev path - but it is only a *fallback*: the
per-user file wins key by key, and is the only file ever written to. Set
MTS_CONFIG to point the whole thing somewhere else (used by the selftest).

Auth is read from Claude Code itself (`claude auth status --json`) rather than
guessed at, with a Keychain probe as a fallback for builds too old to have that
subcommand. Nothing here ever handles a password or a token: signing in happens
inside a real terminal session running `claude auth login`, exactly as it would
in Terminal.app.

Stdlib only.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# macOS application-support folder name. Kept as the original product name so an
# existing install keeps its config across this change - the app is user-facing
# as "Mattdaemon", but the directory is not.
APP_SUPPORT_NAME = "MLSuiteTerminal"

CONFIG_VERSION = 2

# `claude` is a userland install more often than a system one, and a
# Finder-launched .app gets launchd's minimal PATH, so which() alone finds
# nothing. Probe the places the installers actually use.
_CLAUDE_CANDIDATES = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "/usr/bin/claude",
    "~/.bun/bin/claude",
    "~/.npm-global/bin/claude",
    "~/node_modules/.bin/claude",
)

_TMUX_CANDIDATES = (
    "/opt/homebrew/bin/tmux",
    "/usr/local/bin/tmux",
    "/usr/bin/tmux",
    "~/.local/bin/tmux",
)

# `claude auth status` spawns node and takes the better part of a second, and the
# setup screen polls while the user is signing in. Cache it, briefly - long
# enough that polling is free, short enough that a completed login shows up
# almost at once.
_AUTH_TTL = 6.0
_VERSION_TTL = 300.0
_cache_lock = threading.Lock()
_cache = {"auth": (0.0, None), "version": (0.0, None), "bin": (0.0, None)}


# ── config file ────────────────────────────────────────────────────────────

def user_config_path():
    """The one file this app writes. Per-user, outside the bundle."""
    override = os.environ.get("MTS_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/" + APP_SUPPORT_NAME)
    else:
        base = os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "mattdaemon")
    return os.path.join(base, "mts-config.json")


def fallback_config_path():
    """A config next to the source - the checkout/dev path. Read, never written.

    Suppressed when MTS_CONFIG names a file: pointing at a config explicitly
    means *that* config, which is what makes the selftest's throwaway config
    genuinely empty rather than quietly inheriting the developer's own.
    """
    if os.environ.get("MTS_CONFIG"):
        return ""
    return os.path.join(HERE, "mts-config.json")


def _read_json(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config():
    """Merged view: the per-user file overrides the checkout fallback, key by
    key, with `vps` merged one level deeper so a user file that only sets `home`
    does not silently drop a dev checkout's VPS block."""
    cfg = dict(_read_json(fallback_config_path()))
    user = _read_json(user_config_path())
    for key, val in user.items():
        if key == "vps" and isinstance(val, dict) and isinstance(cfg.get("vps"), dict):
            merged = dict(cfg["vps"])
            merged.update(val)
            cfg["vps"] = merged
        else:
            cfg[key] = val
    return cfg


def save_config(patch):
    """Merge `patch` into the per-user config and write it atomically.

    A key whose value is None is removed - that is how the VPS block is cleared.
    The file can hold a dashboard token, so it is written 0600 and the temp file
    is created with the same mode rather than being chmod-ed after the fact.
    Returns the freshly merged full config.
    """
    path = user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    data = _read_json(path)
    for key, val in (patch or {}).items():
        if val is None:
            data.pop(key, None)
        else:
            data[key] = val
    data["version"] = CONFIG_VERSION

    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return load_config()


# ── the two things setup collects ──────────────────────────────────────────

def configured_home(cfg=None):
    """The absolute working directory from config, or "" when unset."""
    cfg = load_config() if cfg is None else cfg
    raw = (cfg.get("home") or "").strip()
    return os.path.abspath(os.path.expanduser(raw)) if raw else ""


def validate_folder(raw):
    """(abs_path, "") for a usable folder, or ("", reason) for a bad one."""
    text = (raw or "").strip()
    if not text:
        return "", "Choose the folder your work lives in."
    path = os.path.abspath(os.path.expanduser(text))
    if not os.path.exists(path):
        return "", "There is no folder at %s." % path
    if not os.path.isdir(path):
        return "", "%s is a file, not a folder." % path
    if not os.access(path, os.R_OK | os.X_OK):
        return "", "No permission to open %s." % path
    return path, ""


def list_dirs(raw):
    """Sub-folders of `raw`, for the in-app folder browser (the no-build path,
    where there is no native picker). Hidden folders are skipped and the list is
    capped - this is a picker, not a file manager."""
    path, err = validate_folder(raw or "~")
    if err:
        return {"ok": False, "error": err}
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=True):
                        entries.append({"name": e.name,
                                        "path": os.path.join(path, e.name)})
                except OSError:
                    continue
    except OSError as e:
        return {"ok": False, "error": str(e)}
    entries.sort(key=lambda d: d["name"].lower())
    parent = os.path.dirname(path)
    return {"ok": True, "path": path,
            "parent": parent if parent and parent != path else "",
            "dirs": entries[:400], "truncated": len(entries) > 400}


# ── Claude Code ────────────────────────────────────────────────────────────

def claude_binary():
    """Absolute path to the `claude` CLI, or "" if it is not installed."""
    with _cache_lock:
        ts, val = _cache["bin"]
        if val is not None and time.time() - ts < _AUTH_TTL:
            return val

    found = shutil.which("claude") or ""
    if not found:
        for cand in _CLAUDE_CANDIDATES:
            p = os.path.expanduser(cand)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                found = p
                break

    with _cache_lock:
        _cache["bin"] = (time.time(), found)
    return found


def tmux_binary():
    """Absolute path to tmux, or "". Defers to terminal_manager's resolver when
    that module is already loaded so both agree on which binary is in play."""
    tm = sys.modules.get("terminal_manager")
    if tm is not None and hasattr(tm, "_resolve_tmux"):
        try:
            return tm._resolve_tmux() or ""
        except Exception:
            pass
    found = shutil.which("tmux") or ""
    if not found:
        for cand in _TMUX_CANDIDATES:
            p = os.path.expanduser(cand)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                found = p
                break
    return found


def _run(cmd, timeout, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    return r


def claude_version():
    binp = claude_binary()
    if not binp:
        return ""
    with _cache_lock:
        ts, val = _cache["version"]
        if val is not None and time.time() - ts < _VERSION_TTL:
            return val
    r = _run([binp, "--version"], 10)
    ver = (r.stdout or "").strip().splitlines()[0] if r and r.stdout else ""
    with _cache_lock:
        _cache["version"] = (time.time(), ver)
    return ver


def _keychain_has_login():
    """Fallback probe: does Claude Code have credentials stored on this machine?

    Metadata only - deliberately no `-w`, so the secret is never requested and
    macOS never puts up a Keychain prompt. Used only when the installed CLI is
    too old for `claude auth status`.
    """
    if sys.platform != "darwin":
        return os.path.isfile(os.path.expanduser("~/.claude/.credentials.json"))
    r = _run(["security", "find-generic-password", "-s", "Claude Code-credentials"], 8)
    return bool(r and r.returncode == 0)


def _parse_auth_json(out):
    """`claude auth status --json` prints JSON, occasionally after a line of
    update chatter - so parse from the first brace rather than the first byte."""
    if not out:
        return None
    start = out.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(out[start:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def auth_state(force=False):
    """Who, if anyone, this machine's Claude Code is signed in as.

    {"loggedIn": bool, "email": str, "method": str, "plan": str, "source": str}
    `source` is "cli" when Claude Code answered for itself and "keychain" when we
    had to fall back - worth surfacing, because the fallback cannot say *who* is
    signed in, only that someone is.
    """
    with _cache_lock:
        ts, val = _cache["auth"]
        if val is not None and not force and time.time() - ts < _AUTH_TTL:
            return val

    state = {"loggedIn": False, "email": "", "method": "", "plan": "", "source": ""}
    binp = claude_binary()
    if binp:
        # Run it from the user's home rather than the project folder: the answer
        # should be about this machine's login, not about whatever a particular
        # repo's settings do. Home always exists, which "" cannot promise.
        r = _run([binp, "auth", "status", "--json"], 30,
                 cwd=os.path.expanduser("~"))
        data = _parse_auth_json(r.stdout) if r else None
        if data is not None:
            state.update({
                "loggedIn": bool(data.get("loggedIn")),
                "email": data.get("email") or "",
                "method": data.get("authMethod") or "",
                "plan": data.get("subscriptionType") or "",
                "source": "cli",
            })
        else:
            state["loggedIn"] = _keychain_has_login()
            state["source"] = "keychain" if state["loggedIn"] else ""

    with _cache_lock:
        _cache["auth"] = (time.time(), state)
    return state


def forget_auth_cache():
    """Drop the cached auth answer so the next read hits Claude Code again.
    Called the moment a login session is started, so the wizard sees the result
    of a sign-in without waiting out the TTL."""
    with _cache_lock:
        _cache["auth"] = (0.0, None)


def login_command(switch=False):
    """The command to type into a session to sign in.

    Absolute path: the tmux shell inherits the .app's PATH, which may not have
    the userland bin directory that `claude` lives in. `--claudeai` is the
    subscription flow (as opposed to console/API billing), which is what this
    app is for. Signing in as someone else needs the current login dropped
    first, or the CLI just reports that it is already signed in.
    """
    binp = claude_binary()
    if not binp:
        return ""
    quoted = "'%s'" % binp.replace("'", "'\\''")
    if switch:
        return "%s auth logout; %s auth login --claudeai" % (quoted, quoted)
    return "%s auth login --claudeai" % quoted


# ── setup state ────────────────────────────────────────────────────────────

def is_complete(cfg=None):
    """True when the app may show the terminal instead of the setup screen.

    Deliberately *not* conditional on being signed in right now. Setup demands a
    login to finish, but a token that expires later should surface as a warning
    in Settings, not as the whole app reverting to a wizard on top of live
    sessions.
    """
    cfg = load_config() if cfg is None else cfg
    if not cfg.get("setupComplete"):
        return False
    home = configured_home(cfg)
    return bool(home) and os.path.isdir(home)


def state(force_auth=False):
    """Everything the setup screen and the settings panel need, in one call."""
    cfg = load_config()
    home = configured_home(cfg)
    tmux = tmux_binary()
    binp = claude_binary()
    auth = auth_state(force=force_auth)
    vps = cfg.get("vps") if isinstance(cfg.get("vps"), dict) else {}

    return {
        "ok": True,
        "needsSetup": not is_complete(cfg),
        "platform": sys.platform,
        "configPath": user_config_path(),
        "home": {
            "path": home,
            "name": os.path.basename(home.rstrip(os.sep)) if home else "",
            "valid": bool(home) and os.path.isdir(home),
            "default": os.path.expanduser("~"),
        },
        "tmux": {"installed": bool(tmux), "path": tmux},
        "claude": {
            "installed": bool(binp),
            "path": binp,
            "version": claude_version(),
            "loggedIn": bool(auth["loggedIn"]),
            "email": auth["email"],
            "method": auth["method"],
            "plan": auth["plan"],
            "source": auth["source"],
        },
        "vps": {
            "configured": bool(vps.get("base_url") and vps.get("token")),
            "base_url": vps.get("base_url") or "",
            "path_map": vps.get("path_map") or {},
            # The token itself is never sent to the page - only whether one is held.
            "hasToken": bool(vps.get("token")),
        },
    }
