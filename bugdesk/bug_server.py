#!/usr/bin/env python3
"""Mattdaemon bug desk: the one place bug reports from every copy of the app land.

Runs on the VPS behind the hub tunnel, at https://hub.mlmediahub.com/mattdaemon/bugs
(a path rule in the tunnel sends that path here and everything else to the
dashboard). It is WRITE-ONLY on purpose: anyone running the app can file a
report, nobody can read one over the internet. Reports are fetched over SSH
with bugdesk/pull.sh, so there is no read API, no token and nothing to leak.

Each report is one JSON file (plus an optional PNG screenshot) under
BUGS_DIR/YYYY-MM/. No IP address is stored: it is used in memory for the rate
limit and then forgotten.

    python3 bug_server.py [--port 8724] [--dir /home/aihub/mattdaemon-bugs]
"""
import argparse
import base64
import binascii
import datetime
import json
import os
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROUTE = "/mattdaemon/bugs"
MAX_BODY = 8 * 1024 * 1024          # a screenshot of a big Retina window fits
MAX_TEXT = 20000
MAX_SHORT = 300
MAX_META = 96 * 1024
MAX_PNG = 6 * 1024 * 1024
PER_IP_PER_HOUR = 10
ALL_PER_DAY = 300
MAX_DISK = 2 * 1024 * 1024 * 1024   # stop taking reports before the disk suffers
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

BUGS_DIR = os.environ.get("BUGS_DIR", "/home/aihub/mattdaemon-bugs")

_lock = threading.Lock()
_by_ip = {}                         # ip -> [timestamps in the last hour]
_all = []                           # timestamps in the last day
_disk = {"at": 0.0, "bytes": 0}


def _client_ip(handler):
    # Behind the tunnel the peer is always localhost; Cloudflare names the
    # real client in this header.
    return (handler.headers.get("CF-Connecting-IP")
            or (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or handler.client_address[0])


def _rate_ok(ip, now=None):
    now = now or time.time()
    with _lock:
        hits = [t for t in _by_ip.get(ip, []) if now - t < 3600]
        _all[:] = [t for t in _all if now - t < 86400]
        if len(hits) >= PER_IP_PER_HOUR or len(_all) >= ALL_PER_DAY:
            _by_ip[ip] = hits
            return False
        hits.append(now)
        _by_ip[ip] = hits
        _all.append(now)
        return True


def _disk_used():
    now = time.time()
    if now - _disk["at"] > 300:
        total = 0
        for root, _dirs, files in os.walk(BUGS_DIR):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        _disk.update(at=now, bytes=total)
    return _disk["bytes"]


def _short(v, limit=MAX_SHORT):
    return str(v or "").strip()[:limit]


def validate(data):
    """(report, png_bytes or None, None) or (None, None, error)."""
    if not isinstance(data, dict):
        return None, None, "expected a JSON object"
    text = str(data.get("text") or "").strip()
    if not text:
        return None, None, "say what happened"
    if len(text) > MAX_TEXT:
        return None, None, "the description is too long"
    meta = data.get("meta") or {}
    if not isinstance(meta, dict) or len(json.dumps(meta)) > MAX_META:
        return None, None, "meta must be a small object"
    png = None
    shot = data.get("screenshot")
    if shot:
        raw = str(shot)
        if raw.startswith("data:image/png;base64,"):
            raw = raw[len("data:image/png;base64,"):]
        try:
            png = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None, None, "the screenshot is not valid base64"
        if not png.startswith(PNG_MAGIC) or len(png) > MAX_PNG:
            return None, None, "the screenshot must be a PNG under 6 MB"
    report = {
        "text": text,
        "expected": _short(data.get("expected"), MAX_TEXT),
        "contact": _short(data.get("contact")),
        "version": _short(data.get("version") or meta.get("version"), 40),
        "meta": meta,
    }
    return report, png, None


def store(report, png, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    month = now.strftime("%Y-%m")
    rid = now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    folder = os.path.join(BUGS_DIR, month)
    os.makedirs(folder, exist_ok=True)
    report = dict(report, id=rid, received=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  screenshot=(rid + ".png") if png else None)
    if png:
        with open(os.path.join(folder, rid + ".png"), "wb") as f:
            f.write(png)
    tmp = os.path.join(folder, "." + rid + ".json.tmp")
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(tmp, os.path.join(folder, rid + ".json"))
    return rid


class Handler(BaseHTTPRequestHandler):
    server_version = "MattdaemonBugDesk/1"

    def log_message(self, fmt, *args):
        # Request lines only; never bodies, never addresses.
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                      fmt % args if "%s" in fmt else fmt))

    def address_string(self):
        return "-"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == ROUTE + "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != ROUTE:
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_BODY:
            self._json(413, {"ok": False, "error": "report too large or empty"})
            return
        raw = self.rfile.read(n)
        if not _rate_ok(_client_ip(self)):
            self._json(429, {"ok": False, "error": "too many reports - try again later"})
            return
        if _disk_used() > MAX_DISK:
            self._json(507, {"ok": False, "error": "the bug desk is full"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json(400, {"ok": False, "error": "not JSON"})
            return
        report, png, err = validate(data)
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        rid = store(report, png)
        _disk["bytes"] += len(raw)
        self._json(200, {"ok": True, "id": rid})


def main():
    global BUGS_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("BUGS_PORT", 8724)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir", default=BUGS_DIR)
    a = ap.parse_args()
    BUGS_DIR = a.dir
    os.makedirs(BUGS_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    print("bug desk on %s:%d -> %s" % (a.host, a.port, BUGS_DIR), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
