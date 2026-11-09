"""In-app updates from GitHub Releases.

The app asks GitHub for the latest release, and when it is newer than what is
running it offers it: version, release notes, "Update now". Saying yes:

  1. downloads the release zip,
  2. checks it is the file GitHub says it is (the SHA-256 GitHub publishes for
     every release asset),
  3. checks it is signed by the SAME certificate as the app that is running -
     the one check that says who made it, not just that it arrived intact,
  4. hands over to a small detached script that quits the app, swaps the
     bundle, and starts the new one. If anything in the swap fails, the old
     bundle goes straight back and is started instead.

Sessions survive because they never lived in the app: they are tmux sessions,
and quitting the app only drops the window onto them.

Step 3 is why updates only work for a copy installed from a release (or built
on the Mac that signs releases). A copy built and signed somewhere else has a
different certificate, so no release can satisfy it, and it is told to update
by hand instead. An ad-hoc signed copy has no certificate at all, so the same.

Everything here is best-effort: no network, no releases, a running-from-source
checkout - all of them answer "nothing to do" rather than raising.
"""
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

REPO = "Sivertsen0007/Mattdaemon"
ASSET_NAME = "Mattdaemon-macOS-arm64.zip"
CHECK_TTL = 6 * 3600          # a background check at most this often
HTTP_TIMEOUT = 15


def _feed_url():
    return (os.environ.get("MATTDAEMON_UPDATE_FEED")
            or "https://api.github.com/repos/%s/releases/latest" % REPO)


# ── where things are ───────────────────────────────────────────────────────

def updates_dir():
    """Downloads, the swap script, its log and a little state, per user."""
    override = os.environ.get("MATTDAEMON_UPDATES_DIR")
    if override:
        base = override
    else:
        import setup  # sibling module; keeps one idea of "the config folder"
        base = os.path.join(os.path.dirname(setup.user_config_path()), "updates")
    os.makedirs(base, exist_ok=True)
    return base


def current_version():
    """VERSION next to this file - the repo root, or the bundle's Resources."""
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def app_bundle_path():
    """The .app this engine runs inside, or None when running from source."""
    override = os.environ.get("MATTDAEMON_APP_PATH")
    if override:
        return override
    resources = HERE
    contents = os.path.dirname(resources)
    app = os.path.dirname(contents)
    if (os.path.basename(resources) == "Resources"
            and os.path.basename(contents) == "Contents"
            and app.endswith(".app")):
        return app
    return None


def parse_version(v):
    """'v0.2.10' -> (0, 2, 10); anything else -> None."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


# ── small persisted state (skipped version, "you were just updated") ───────

def _state_path():
    return os.path.join(updates_dir(), "state.json")


def _load_state():
    try:
        with open(_state_path()) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data):
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _state_path())


def skip_version(version):
    st = _load_state()
    st["skipped"] = version
    _save_state(st)
    return {"ok": True}


def ack_updated():
    st = _load_state()
    st.pop("justUpdated", None)
    _save_state(st)
    return {"ok": True}


# ── checking ───────────────────────────────────────────────────────────────

_lock = threading.Lock()
_check_cache = {"at": 0.0, "release": None, "error": None}
_job = {"phase": "idle", "progress": 0, "error": None, "version": None}


def _http_get(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mattdaemon-updater",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch_latest():
    """The latest published release with our zip on it, or None."""
    try:
        with _http_get(_feed_url()) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:          # no releases yet
            return None, None
        return None, "GitHub answered %d" % e.code
    except Exception as e:
        return None, "could not reach GitHub (%s)" % e.__class__.__name__
    if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
        return None, None
    version = (rel.get("tag_name") or "").lstrip("v")
    if not parse_version(version):
        return None, None
    assets = rel.get("assets") or []
    zip_asset = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if not zip_asset:
        return None, None
    sha = None
    digest = zip_asset.get("digest") or ""
    if digest.startswith("sha256:"):
        sha = digest[len("sha256:"):].lower()
    if not sha:
        # Older GitHub answers have no digest; a sidecar file does the same job.
        side = next((a for a in assets if a.get("name") == ASSET_NAME + ".sha256"), None)
        if side:
            try:
                with _http_get(side["browser_download_url"]) as r:
                    sha = r.read().decode("utf-8").split()[0].lower()
            except Exception:
                sha = None
    return {
        "version": version,
        "notes": (rel.get("body") or "").strip(),
        "url": zip_asset.get("browser_download_url"),
        "size": int(zip_asset.get("size") or 0),
        "sha256": sha,
        "page": rel.get("html_url") or "https://github.com/%s/releases/latest" % REPO,
        "published": rel.get("published_at"),
    }, None


def _cannot_install_reason():
    """Why this copy cannot update itself, or None if it can."""
    app = app_bundle_path()
    if not app:
        return "running from source - pull and rebuild instead"
    if not os.access(os.path.dirname(app), os.W_OK):
        return "no permission to replace %s" % app
    req = _designated_requirement(app)
    if not req:
        return "this copy is not signed, so a download cannot be checked against it"
    if "cdhash" in req:
        return "this copy is ad-hoc signed, so a download cannot be checked against it"
    return None


def status(force=False):
    """What the page needs: running version, latest, whether to offer it."""
    with _lock:
        stale = time.time() - _check_cache["at"] > CHECK_TTL
        busy = _job["phase"] in ("downloading", "verifying", "installing")
    if (force or stale) and not busy:
        rel, err = _fetch_latest()
        with _lock:
            _check_cache.update(at=time.time(), release=rel, error=err)
    with _lock:
        rel = _check_cache["release"]
        err = _check_cache["error"]
        job = dict(_job)
    cur = current_version()
    st = _load_state()
    available = bool(rel and is_newer(rel["version"], cur))
    out = {
        "ok": True,
        "current": cur,
        "latest": rel["version"] if rel else None,
        "available": available,
        "skipped": bool(available and st.get("skipped") == rel["version"]),
        "release": rel if available else None,
        "checkedAt": _check_cache["at"],
        "error": err,
        "job": job,
        "canInstall": None,
        "cannotInstall": None,
    }
    if available:
        reason = _cannot_install_reason()
        out["canInstall"] = reason is None
        out["cannotInstall"] = reason
    ju = st.get("justUpdated")
    if isinstance(ju, dict) and ju.get("version") == cur:
        out["justUpdated"] = ju
    return out


# ── signature checks ───────────────────────────────────────────────────────

def _designated_requirement(app):
    """The running app's designated requirement - the rule a replacement must
    satisfy to count as the same app signed by the same certificate."""
    try:
        r = subprocess.run(["codesign", "-d", "-r-", app],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        # Ad-hoc signatures print it as a comment ("# designated => cdhash ...").
        line = line.strip().lstrip("#").strip()
        if line.startswith("designated =>"):
            return line[len("designated =>"):].strip()
    return None


def verify_bundle(new_app, current_app, expect_version=None):
    """None if new_app may replace current_app, else the reason it may not."""
    if not os.path.isdir(new_app):
        return "the download has no Mattdaemon.app in it"
    try:
        with open(os.path.join(new_app, "Contents", "Info.plist"), "rb") as f:
            new_info = plistlib.load(f)
        with open(os.path.join(current_app, "Contents", "Info.plist"), "rb") as f:
            cur_info = plistlib.load(f)
    except Exception:
        return "the download's Info.plist cannot be read"
    if new_info.get("CFBundleIdentifier") != cur_info.get("CFBundleIdentifier"):
        return "the download is a different app"
    if expect_version and new_info.get("CFBundleShortVersionString") != expect_version:
        return "the download says it is %s, not %s" % (
            new_info.get("CFBundleShortVersionString"), expect_version)
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", new_app],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return "the download's signature is broken"
    req = _designated_requirement(current_app)
    if not req or "cdhash" in req:
        return "this copy has no certificate to check the download against"
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                        "-R=" + req, new_app], capture_output=True, text=True)
    if r.returncode != 0:
        return "the download is not signed by the same certificate as this copy"
    return None


# ── applying ───────────────────────────────────────────────────────────────

SWAP_SCRIPT = r"""#!/bin/bash
# Mattdaemon update: quit the app, swap the bundle, start the new one.
# Written and started by updater.py; runs detached so it outlives the app.
set -u
APP="$1"; NEW="$2"; BAK="$3"; PID="$4"; BID="$5"
OPEN="${MATTDAEMON_OPEN_CMD:-open}"
echo "$(date '+%F %T') update: $NEW -> $APP"
sleep 1    # let the page get its answer before the app goes
if [ -n "$BID" ]; then
    osascript -e "if application id \"$BID\" is running then tell application id \"$BID\" to quit" >/dev/null 2>&1 || true
fi
for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$PID" 2>/dev/null; then kill "$PID" 2>/dev/null; sleep 2; fi
rm -rf "$BAK"
if ! mv "$APP" "$BAK"; then
    echo "update: could not move the old app aside - left as it was"
    $OPEN "$APP"; exit 1
fi
if ditto --noextattr --noqtn "$NEW" "$APP" && codesign --verify --deep --strict "$APP"; then
    xattr -rd com.apple.FinderInfo "$APP" 2>/dev/null || true
    echo "update: installed"
    rm -rf "$NEW"
    $OPEN "$APP"
else
    echo "update: install failed - putting the old app back"
    rm -rf "$APP"
    mv "$BAK" "$APP"
    $OPEN "$APP"
    exit 1
fi
"""


def _set_job(**kw):
    with _lock:
        _job.update(kw)


def apply_update(app_pid=None, wait=False):
    """Start downloading and installing the latest release.

    Returns at once (the page polls status for progress) unless `wait`, which
    the tests use. `app_pid` is the process to wait for before swapping -
    the native app, i.e. this server's parent.
    """
    with _lock:
        if _job["phase"] in ("downloading", "verifying", "installing"):
            return {"ok": False, "error": "an update is already running"}
        rel = _check_cache["release"]
    if not rel or not is_newer(rel["version"], current_version()):
        return {"ok": False, "error": "no newer version to install"}
    reason = _cannot_install_reason()
    if reason:
        return {"ok": False, "error": reason}
    if not rel.get("sha256"):
        return {"ok": False, "error": "the release has no checksum to verify the download against"}
    _set_job(phase="downloading", progress=0, error=None, version=rel["version"])
    t = threading.Thread(target=_run_update, args=(rel, app_pid or os.getppid()),
                         daemon=True)
    t.start()
    if wait:
        t.join()
    return {"ok": True}


def _run_update(rel, app_pid):
    app = app_bundle_path()
    base = updates_dir()
    work = tempfile.mkdtemp(prefix="dl-", dir=base)
    try:
        # 1. download, hashing as it arrives
        zpath = os.path.join(work, ASSET_NAME)
        h = hashlib.sha256()
        got = 0
        with _http_get(rel["url"], timeout=60) as r, open(zpath, "wb") as f:
            total = rel.get("size") or int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    _set_job(progress=min(99, int(got * 100 / total)))
        _set_job(phase="verifying", progress=100)
        if h.hexdigest() != rel["sha256"]:
            raise RuntimeError("the download does not match GitHub's checksum")

        # 2. unpack and check who made it
        stage = os.path.join(work, "stage")
        os.makedirs(stage)
        r = subprocess.run(["ditto", "-x", "-k", zpath, stage],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("the download could not be unpacked")
        new_app = os.path.join(stage, "Mattdaemon.app")
        subprocess.run(["xattr", "-cr", new_app], capture_output=True)
        why = verify_bundle(new_app, app, expect_version=rel["version"])
        if why:
            raise RuntimeError(why)

        # 3. hand over to the swap script, which outlives us
        ready = os.path.join(base, "Mattdaemon-%s.app" % rel["version"])
        shutil.rmtree(ready, ignore_errors=True)
        subprocess.run(["ditto", new_app, ready], check=True, capture_output=True)
        backup = os.path.join(base, "Mattdaemon-previous.app")
        script = os.path.join(base, "swap.sh")
        with open(script, "w") as f:
            f.write(SWAP_SCRIPT)
        os.chmod(script, 0o755)
        bundle_id = ""
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                bundle_id = plistlib.load(f).get("CFBundleIdentifier", "")
        except Exception:
            pass
        if os.environ.get("MATTDAEMON_UPDATE_NO_QUIT"):
            bundle_id = ""        # tests: never ask a real app to quit
        st = _load_state()
        st["justUpdated"] = {"version": rel["version"], "from": current_version(),
                             "notes": rel.get("notes", "")}
        st.pop("skipped", None)
        _save_state(st)
        _set_job(phase="installing")
        log = open(os.path.join(base, "update.log"), "a")
        subprocess.Popen(["/bin/bash", script, app, ready, backup, str(app_pid), bundle_id],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    except Exception as e:
        _set_job(phase="error", error=str(e) or e.__class__.__name__)
    finally:
        shutil.rmtree(work, ignore_errors=True)
