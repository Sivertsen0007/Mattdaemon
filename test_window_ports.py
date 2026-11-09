#!/usr/bin/env python3
"""Each pop-out window gets a port of its own (see window_port in server.py).

WebKit shares ~6 connections per host:port across every window in the app, and
each window keeps a stream open, so pop-outs on one port starved each other's
keystrokes. This proves the ports: one per session, stable when it pops out
again, each a full copy of the engine, and capped.

Run: python3 test_window_ports.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp(prefix="mts-winports-")
os.environ["MTS_CONFIG"] = os.path.join(tmp, "mts-config.json")
import server  # noqa: E402

passed = failed = 0


def check(cond, label):
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(("  ✓ " if cond else "  ✗ ") + label)


def get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5) as r:
        return r.status, r.read()


httpd = server._make_server(0)
main_port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

a = json.loads(get(main_port, "/api/window/port?sid=aaa")[1])
b = json.loads(get(main_port, "/api/window/port?sid=bbb")[1])
a2 = json.loads(get(main_port, "/api/window/port?sid=aaa")[1])
check(a["ok"] and b["ok"], "the engine hands out window ports")
check(a["port"] != b["port"] and main_port not in (a["port"], b["port"]),
      "each window gets its own port, never the main one")
check(a2["port"] == a["port"], "a session that pops out again gets the port it had")
st, body = get(a["port"], "/")
check(st == 200 and b"<html" in body.lower(), "the window port serves the page")
st, body = get(a["port"], "/api/update/status")
check(st == 200 and json.loads(body).get("ok"), "and the same API as the main port")
ports = {json.loads(get(main_port, "/api/window/port?sid=s%d" % i)[1])["port"] for i in range(30)}
check(len(server._window_servers) == server._WINDOW_PORTS_MAX,
      "listeners are capped at %d" % server._WINDOW_PORTS_MAX)

print("\nAll %d checks passed." % passed if not failed
      else "\n%d of %d checks FAILED." % (failed, passed + failed))
sys.exit(1 if failed else 0)
