#!/usr/bin/env python3
"""A window that stops reading for a while gets the present, not a backlog.

macOS pauses a pop-out window that sits behind others. Its output stream kept
filling meanwhile - up to thousands of frames queued in the engine and the
socket - and on waking the window drew every one of them before the frame
showing what you had just typed. That read as typing lag of seconds, at worst a
minute.

This opens a real stream onto a real tmux session that prints without pause,
stops reading for 8 seconds, and then measures how stale the output is when
reading resumes. The engine must say "you fell behind" (a stale desync) almost
at once instead of feeding the backlog through.

Run: python3 test_stale_stream.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp(prefix="mts-stale-")
os.environ["MTS_CONFIG"] = os.path.join(tmp, "mts-config.json")
os.environ["MTS_TMUX_SOCKET"] = "mtd-staletest-%d" % os.getpid()
import server  # noqa: E402

passed = failed = 0


def check(cond, label):
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(("  ✓ " if cond else "  ✗ ") + label)


tm = server._import_manager()
if not tm._tmux_available():
    print("SKIP: no tmux")
    sys.exit(0)
SID = "staletest"
tm._tmux("new-session", "-d", "-s", tm._sname(SID), "-x", "120", "-y", "40",
         "sh -c 'while :; do date; seq 1 300; done'")

httpd = server._make_server(0)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

s = socket.create_connection(("127.0.0.1", port))
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
s.sendall(("GET /api/terminal/stream-multi?sids=%s HTTP/1.1\r\nHost: x\r\n\r\n" % SID).encode())
f = s.makefile("rb")


def next_event(deadline):
    while time.time() < deadline:
        line = f.readline()
        if not line:
            return None
        if line.startswith(b"data: "):
            return json.loads(line[6:])
    return None


try:
    print("\nLIVE")
    end = time.time() + 5
    fresh = False
    while time.time() < end:
        e = next_event(end)
        if e and e.get("type") == "output" and time.time() * 1000 - e.get("t", 0) < 500:
            fresh = True
            break
    check(fresh, "output arrives live, stamped with when it was made")

    print("\nPAUSED FOR 8 SECONDS")
    time.sleep(8)
    resumed = time.time()
    got_desync = None
    dropped = 0
    first_age = None
    while time.time() - resumed < 15:
        e = next_event(resumed + 15)
        if e is None:
            break
        if e.get("type") == "desync" and e.get("reason") == "stale":
            got_desync = time.time() - resumed
            dropped = e.get("dropped", 0)
            break
        if e.get("type") == "output" and first_age is None and e.get("t"):
            first_age = time.time() * 1000 - e["t"]
    check(first_age is not None and first_age > 3000,
          "the first frames after the pause are old (%s ms) - the page skips those"
          % (int(first_age) if first_age is not None else "?"))
    check(got_desync is not None and got_desync < 2,
          "the engine says it fell behind within 2 s of resuming (%s)"
          % ("%.2fs" % got_desync if got_desync is not None else "never"))
    check(dropped > 0, "and throws away the backlog it was holding (%d frames)" % dropped)
    tail = f.readline()
    check(not tail or not tail.strip() or tail.startswith(b"0"),
          "then ends the stream, so the window re-opens onto the present")
finally:
    s.close()
    httpd.shutdown()
    tm._tmux("kill-server")

print("\nAll %d checks passed." % passed if not failed
      else "\n%d of %d checks FAILED." % (failed, passed + failed))
sys.exit(1 if failed else 0)
