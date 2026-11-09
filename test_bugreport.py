#!/usr/bin/env python3
"""End to end: the app's bug report reaching a real bug desk.

A real bugdesk/bug_server.py on a spare port with a temp folder, and the real
bugreport.py posting to it over HTTP. It proves:

  a report lands as one JSON file in this month's folder,
  a screenshot lands as a real PNG next to it, and a fake one is refused,
  the app adds versions and timings but nothing from a session,
  an empty report is refused,
  with the desk down the report waits in the outbox and goes when it is back,
  one sender is held to the hourly limit.

Run: python3 test_bugreport.py
"""
import base64
import glob
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
passed = failed = 0
# 1x1 transparent PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print("  ✓ " + label)
    else:
        failed += 1
        print("  ✗ " + label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_desk(port, folder):
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "bugdesk", "bug_server.py"),
                             "--port", str(port), "--dir", folder],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/mattdaemon/bugs/health" % port, timeout=1)
            return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("bug desk did not start")


def reports(folder):
    return sorted(glob.glob(os.path.join(folder, "*", "*.json")))


def main():
    work = tempfile.mkdtemp(prefix="mtd-bugtest-")
    desk_dir = os.path.join(work, "desk")
    port = free_port()
    os.environ["MATTDAEMON_BUG_URL"] = "http://127.0.0.1:%d/mattdaemon/bugs" % port
    os.environ["MATTDAEMON_BUGS_DIR"] = os.path.join(work, "app")
    import bugreport as br
    importlib.reload(br)
    desk = start_desk(port, desk_dir)
    try:
        print("\nSENDING")
        r = br.submit({"text": "Scrolling stops in a pop-out", "expected": "It scrolls",
                       "contact": "tester", "meta": {"page": "solo", "panes": 1,
                       "screenText": "SECRET terminal text"}},
                      {"local": 3, "vps": False})
        check(r.get("ok") and r.get("id") and not r.get("queued"), "a report is accepted (%s)" % r)
        files = reports(desk_dir)
        check(len(files) == 1 and os.path.basename(os.path.dirname(files[0])) == time.strftime("%Y-%m", time.gmtime()),
              "it lands as one file in this month's folder")
        rep = json.load(open(files[0]))
        check(rep["text"] == "Scrolling stops in a pop-out" and rep["expected"] == "It scrolls"
              and rep["contact"] == "tester", "what the user wrote is kept as written")
        m = rep["meta"]
        check(m.get("version") and m.get("macos") and m.get("sessions") == {"local": 3, "vps": False},
              "versions and session counts are added")
        check("SECRET" not in json.dumps(rep), "nothing the page slips in beyond the known fields gets through")

        print("\nSCREENSHOTS")
        shot = "data:image/png;base64," + base64.b64encode(PNG).decode()
        r = br.submit({"text": "with a picture", "screenshot": shot})
        rid = r.get("id")
        pngs = glob.glob(os.path.join(desk_dir, "*", "%s.png" % rid))
        check(r.get("ok") and pngs and open(pngs[0], "rb").read() == PNG, "a screenshot lands as the same PNG")
        r = br.submit({"text": "fake picture",
                       "screenshot": "data:image/png;base64," + base64.b64encode(b"not a png").decode()})
        check(not r.get("ok") and "PNG" in (r.get("error") or ""), "something that is not a PNG is refused")

        print("\nREFUSING")
        r = br.submit({"text": "   "})
        check(not r.get("ok"), "an empty report is refused")

        print("\nOFFLINE")
        desk.terminate(); desk.wait()
        before = len(reports(desk_dir))
        r = br.submit({"text": "written while the desk was down"})
        check(r.get("ok") and r.get("queued") and br.pending() == 1, "with the desk down it waits in the outbox")
        desk = start_desk(port, desk_dir)
        n = br.flush_outbox()
        check(n == 1 and br.pending() == 0 and len(reports(desk_dir)) == before + 1,
              "when the desk is back it is sent and the outbox is empty")

        print("\nLIMITS")
        results = [br.submit({"text": "spam %d" % i}) for i in range(12)]
        # The restart reset the desk's counter, and the flush above used one.
        accepted = sum(1 for x in results if x.get("ok") and not x.get("queued"))
        check(accepted == 9, "one sender gets 10 an hour, the rest wait (%d accepted)" % accepted)
        check(br.pending() == 3, "and the extra ones wait in the outbox instead of being lost")
    finally:
        desk.terminate()
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d of %d checks passed." % (passed, passed + failed) if not failed
          else "\n%d of %d checks FAILED." % (failed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
