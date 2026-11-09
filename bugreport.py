"""Send a bug report from this copy of the app to the bug desk.

The page collects what the user writes (and, only if they tick the box, a
screenshot they can see before sending). This adds the technical facts that
make a report useful - app and macOS version, the Mac's memory state, the
connection timings from the latency log - and posts it to the bug desk
(bugdesk/bug_server.py on the VPS). Nothing from a terminal session is ever
added: no screen text, no session names, no paths.

If the desk cannot be reached the report waits in an outbox and goes with the
next attempt, so a report written on a plane is not lost.
"""
import glob
import json
import os
import platform
import subprocess
import threading
import time
import urllib.error
import urllib.request

BUG_URL = os.environ.get("MATTDAEMON_BUG_URL", "https://hub.mlmediahub.com/mattdaemon/bugs")
LATENCY_LOG = os.path.expanduser("~/Library/Logs/mattdaemon-latency.log")
LATENCY_LINES = 60                  # about the last hour
OUTBOX_MAX = 20
TIMEOUT = 25

_send_lock = threading.Lock()


def outbox_dir():
    override = os.environ.get("MATTDAEMON_BUGS_DIR")
    if override:
        base = override
    else:
        import setup
        base = os.path.join(os.path.dirname(setup.user_config_path()), "bugs")
    path = os.path.join(base, "outbox")
    os.makedirs(path, exist_ok=True)
    return path


def _version():
    try:
        import updater
        return updater.current_version()
    except Exception:
        return "unknown"


def _macos():
    try:
        return subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or platform.mac_ver()[0]
    except Exception:
        return platform.mac_ver()[0] or platform.platform()


def _latency_tail():
    try:
        with open(LATENCY_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()[-LATENCY_LINES:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def build(page_data, sessions=None):
    """The report as it goes over the wire. `page_data` is what the page sent."""
    meta = page_data.get("meta") if isinstance(page_data.get("meta"), dict) else {}
    meta = {k: meta[k] for k in ("page", "window", "panes", "focus", "diag", "userAgent")
            if k in meta}
    meta.update({
        "version": _version(),
        "macos": _macos(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "sessions": sessions or {},
        "latency": _latency_tail(),
        "sentAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    report = {
        "text": str(page_data.get("text") or "").strip(),
        "expected": str(page_data.get("expected") or "").strip(),
        "contact": str(page_data.get("contact") or "").strip(),
        "version": meta["version"],
        "meta": meta,
    }
    if page_data.get("screenshot"):
        report["screenshot"] = page_data["screenshot"]
    return report


def _post(report):
    body = json.dumps(report).encode("utf-8")
    req = urllib.request.Request(BUG_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        # The hub tunnel turns urllib's default User-Agent away (see
        # _vps_headers in server.py), so name the product.
        "User-Agent": "Mattdaemon/%s (macOS)" % report.get("version", "?"),
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _queue(report):
    box = outbox_dir()
    files = sorted(glob.glob(os.path.join(box, "*.json")))
    for old in files[:max(0, len(files) - OUTBOX_MAX + 1)]:
        os.remove(old)
    path = os.path.join(box, "%d-%d.json" % (int(time.time() * 1000), os.getpid()))
    with open(path + ".tmp", "w") as f:
        json.dump(report, f)
    os.replace(path + ".tmp", path)


def submit(page_data, sessions=None):
    text = str(page_data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "Say what happened first."}
    report = build(page_data, sessions)
    with _send_lock:
        try:
            res = _post(report)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:
                msg = None
            if 400 <= e.code < 500 and e.code != 429:
                # The desk read it and said no - sending it again will not help.
                return {"ok": False, "error": msg or "The bug desk refused the report (%d)." % e.code}
            _queue(report)
            return {"ok": True, "queued": True}
        except (urllib.error.URLError, OSError, ValueError):
            _queue(report)
            return {"ok": True, "queued": True}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "The bug desk did not take it."}
    threading.Thread(target=flush_outbox, daemon=True).start()
    return {"ok": True, "id": res.get("id")}


def flush_outbox():
    """Send whatever is waiting. Returns how many went."""
    sent = 0
    with _send_lock:
        for path in sorted(glob.glob(os.path.join(outbox_dir(), "*.json"))):
            try:
                with open(path) as f:
                    report = json.load(f)
            except (OSError, ValueError):
                os.remove(path)
                continue
            try:
                res = _post(report)
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code != 429:
                    os.remove(path)     # refused for good; do not retry forever
                    continue
                break
            except (urllib.error.URLError, OSError, ValueError):
                break                   # still offline; try again later
            if res.get("ok"):
                os.remove(path)
                sent += 1
    return sent


def pending():
    try:
        return len(glob.glob(os.path.join(outbox_dir(), "*.json")))
    except OSError:
        return 0


def start_background_flush(every=1800):
    def loop():
        time.sleep(30)
        while True:
            try:
                if pending():
                    flush_outbox()
            except Exception:
                pass
            time.sleep(every)
    threading.Thread(target=loop, daemon=True, name="bug-outbox").start()
