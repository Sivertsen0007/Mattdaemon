#!/usr/bin/env python3
"""Real end-to-end test for user folders in the session list.

Same harness as test_focus.py: a real server, real shells on a scratch tmux
socket (MTS_TMUX_SOCKET), and a real browser driving the real page. It proves:

  the old single "Saved for later" list migrates into a folder of that name,
  folders can be made, renamed, filled (menu and drag) and deleted,
  a shut folder still shows a red dot for a session that wants you,
  and a session opened from a folder stays open across polls.

Run: python3 test_folders.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Imported first: test_focus points MTS_TMUX_SOCKET at its own socket on import.
from test_focus import _chromium  # noqa: E402

SOCKET = "mlsterm-folders-test"
os.environ["MTS_TMUX_SOCKET"] = SOCKET

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {label}")
    else:
        failed += 1
        print(f"  ✗ {label}")


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed - skipping (pip3 install playwright)")
        return 0

    workdir = tempfile.mkdtemp(prefix="mts-foldertest-")
    os.environ["MTS_CONFIG"] = os.path.join(workdir, "mts-config.json")
    os.environ["TERMINAL_HOME"] = workdir

    import server
    server.setup.save_config({"setupComplete": True, "home": workdir})
    tm = server._import_manager()
    tm.START_DIR = workdir
    if not tm._tmux_available():
        print("FAIL: tmux not available")
        return 1
    if tm.TMUX_SOCKET != SOCKET:
        print(f"FAIL: refusing to run against the real socket ({tm.TMUX_SOCKET})")
        return 1

    httpd = server._make_server(0)
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path, obj):
        req = urllib.request.Request(base + path, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def tmux(*args):
        import subprocess
        return subprocess.run([tm.TMUX_BIN, "-L", SOCKET, *args],
                              capture_output=True, text=True, timeout=10)

    sids = {}
    try:
        for name in ("Alpha", "Bravo", "Charlie", "Delta"):
            r = post("/api/terminal/start", {"name": name, "autostart": False})
            if not r.get("ok"):
                print(f"FAIL: could not start {name}: {r}")
                return 1
            sids[name] = r["session"]["id"]
        time.sleep(1.0)
        now = int(time.time())
        tmux("set-option", "-t", "web-" + sids["Bravo"], "@webstate", f"attention:{now}")
        tmux("set-option", "-t", "web-" + sids["Bravo"], "@webneed",
             f"question|{now - 30}|which database?")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium() or None)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            def ls(key):
                return page.evaluate(f"() => JSON.parse(localStorage.getItem('mts.{key}'))")

            def row(name):
                return page.locator(".sess", has=page.locator(".sname", has_text=name))

            def folder(name):
                return page.locator(".folder", has=page.locator(".fname", has_text=name))

            # Seed the pre-folder state: Alpha stashed in the old saved list.
            page.goto(base + "/", wait_until="domcontentloaded")
            page.evaluate("""a => { localStorage.removeItem('mts.folders');
                                    localStorage.removeItem('mts.folderOf');
                                    localStorage.setItem('mts.saved', JSON.stringify([a])); }""",
                          sids["Alpha"])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("() => document.querySelectorAll('.folder').length >= 1",
                                   timeout=30000)
            check(not errors, f"the page runs clean{' - ' + errors[0] if errors else ''}")

            print("\nTHE OLD SAVED LIST MIGRATES")
            check(folder("Saved for later").count() == 1, "a 'Saved for later' folder exists")
            check(folder("Saved for later").locator(".count").inner_text() == "1",
                  "with Alpha counted in it")
            check(row("Alpha").count() == 0, "and Alpha is out of the loose list while it is shut")
            fo = ls("folderOf")
            check(fo and fo.get(sids["Alpha"]), "the membership is persisted")

            print("\nMAKE AND NAME A FOLDER")
            page.click(".newf")
            page.wait_for_selector('.folder .fname[contenteditable="true"]')
            page.keyboard.press("Meta+a")
            page.keyboard.type("Website loop")
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
            check(folder("Website loop").count() == 1, "typing names the new folder")
            check(any(f["name"] == "Website loop" for f in ls("folders")),
                  "and the name is saved")

            print("\nMOVE BY MENU, MOVE BY DRAG")
            row("Bravo").click(button="right")
            page.locator("#ctxFolders button", has_text="Website loop").click()
            page.wait_for_timeout(200)
            check(ls("folderOf").get(sids["Bravo"]) == ls("folders")[-1]["id"],
                  "right-click > Website loop moves Bravo in")
            row("Charlie").drag_to(folder("Website loop"))
            page.wait_for_timeout(200)
            check(ls("folderOf").get(sids["Charlie"]) == ls("folders")[-1]["id"],
                  "dragging Charlie onto the folder moves it in")

            print("\nA SHUT FOLDER STILL SAYS WHO WANTS YOU")
            if "open" in (folder("Website loop").get_attribute("class") or ""):
                folder("Website loop").click()
            page.wait_for_function(
                "() => [...document.querySelectorAll('.folder')].some(f => "
                "f.textContent.includes('Website loop') && f.querySelector('.mini .s-attention'))",
                timeout=15000)
            check(True, "the shut folder shows Bravo's red dot")
            check(folder("Website loop").locator(".count").inner_text() == "2", "and counts 2")

            print("\nOPEN A SESSION FROM ITS FOLDER")
            folder("Website loop").click()
            row("Charlie").click()
            page.wait_for_timeout(7000)   # two polls at least
            check(page.evaluate("() => window.__mtsActive()") == sids["Charlie"],
                  "it is still the open session after the polls")
            check(ls("folderOf").get(sids["Charlie"]), "and it stays in its folder")

            print("\nA QUIET FOLDER IS OUT OF MIND")
            page.wait_for_function(
                f"() => window.__mtsQueue().some(i => i.sid === {json.dumps(sids['Bravo'])})",
                timeout=15000)
            check(True, "before: Bravo's question is on the Focus queue")
            folder("Website loop").click(button="right")
            page.click('#folderMenu [data-act="quiet"]')
            page.wait_for_timeout(200)
            check(row("Bravo").locator(".sdot.s-quiet").count() == 1,
                  "Bravo's dot goes quiet")
            check(row("Bravo").locator(".need").count() == 0, "and loses its red chip")
            check(folder("Website loop").locator(".quiet").count() == 1,
                  "the folder says it is quiet")
            folder("Website loop").click()   # shut it
            check(folder("Website loop").locator(".mini").count() == 0,
                  "and shut, it shows no dots at all")
            page.wait_for_function(
                f"() => !window.__mtsQueue().some(i => i.sid === {json.dumps(sids['Bravo'])})",
                timeout=15000)
            check(True, "Bravo drops off the Focus queue")
            check(any(f.get("quiet") for f in ls("folders")), "quiet is saved")
            folder("Website loop").click(button="right")
            page.click('#folderMenu [data-act="quiet"]')
            page.wait_for_timeout(200)
            check(folder("Website loop").locator(".mini .s-attention").count() == 1,
                  "turned back on, the red dot returns")
            folder("Website loop").click()   # open again for the rename below

            print("\nRENAME AND DELETE")
            folder("Website loop").locator(".fname").dblclick()
            page.keyboard.press("Meta+a")
            page.keyboard.type("SEO loop")
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
            check(folder("SEO loop").count() == 1, "double-click renames")
            folder("SEO loop").click(button="right")
            page.click('#folderMenu [data-act="delete"]')
            page.wait_for_timeout(300)
            check(folder("SEO loop").count() == 0, "delete removes the folder")
            fo = ls("folderOf")
            check(sids["Bravo"] not in fo and sids["Charlie"] not in fo,
                  "and its sessions are back in the loose list")
            check(row("Bravo").count() == 1 and row("Charlie").count() == 1,
                  "drawn there again")
            check(all(tmux("has-session", "-t", "web-" + sids[n]).returncode == 0
                      for n in sids), "and no tmux session was stopped along the way")
            print("\nCMD-ARROWS WALK THE LOOSE SESSIONS IN FOCUS")
            page.click("#btnFocus")
            loose = page.evaluate("""() => [...document.querySelectorAll('#sessList .sess, .sess-list .sess')]
                .map(r => r.dataset.s)""")
            loose = [x for i, x in enumerate(loose) if x and x not in loose[:i]
                     and x != sids["Alpha"]]
            check(sids["Alpha"] not in loose and len(loose) == 3,
                  f"Alpha (in a folder) is not in the walk ({len(loose)} loose)")
            page.evaluate("sid => window.__mtsOpen(sid)", loose[0])
            page.wait_for_timeout(300)
            visited = []
            for _ in range(3):
                page.keyboard.press("Meta+ArrowDown")
                page.wait_for_timeout(400)
                visited.append(page.evaluate("() => window.__mtsActive()"))
            check(visited == [loose[1], loose[2], loose[0]],
                  "down, down, down - and from the bottom back to the top")
            keys_before = page.evaluate("() => window.__mtsDiag().keys")
            page.keyboard.press("Meta+ArrowUp")
            page.wait_for_timeout(400)
            check(page.evaluate("() => window.__mtsDiag().keys") == keys_before,
                  "the shortcut types nothing into the session")
            check(page.evaluate("() => window.__mtsActive()") == loose[2],
                  "up from the top wraps to the bottom")
            check(sids["Alpha"] not in visited, "the folder session is never visited")

            print("\nTHE LATENCY LOG")
            pane = page.locator(".tile .xterm").first
            pane.click()
            k0 = page.evaluate("() => window.__mtsDiag().keys")
            page.keyboard.type("echo hi")
            page.wait_for_timeout(1500)
            d = page.evaluate("() => window.__mtsDiag()")
            check(d["keys"] - k0 == 7, f"keystrokes are counted ({d['keys'] - k0} for 'echo hi')")
            check(len(d["post"]["local"]) >= 1, "each input POST is timed")
            check(len(d["echo"]["local"]) >= 1 and d["echo"]["local"][0] < 5000,
                  f"keystroke-to-screen is measured ({d['echo']['local'][:3]} ms)")
            server.LAT_LOG = os.path.join(workdir, "lat.log")
            post("/api/diag/latency", dict(d, page="main", lagMax=1500,
                 stream={"stalls": 1}, echo={"local": [40, 2500], "vps": []}))
            with open(server.LAT_LOG) as fh:
                line = json.loads(fh.readline())
            check(line["echo"]["local"]["over1s"] == 1 and "swap_used_mb" in line["mem"],
                  "the server writes one line with the page's figures and the Mac's memory")
            check(line["server_input"]["local"] is not None,
                  "and its own time for the input calls it served")
            rep = server.latency_report(7, server.LAT_LOG)
            check("Slow minutes (an echo over 1s): 1 of 1" in rep and "page itself froze 1" in rep,
                  "--latency-report reads it back and names the freeze")
            check(not errors, f"still clean{' - ' + errors[0] if errors else ''}")
            browser.close()
    finally:
        tmux("kill-server")
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{'All ' + str(passed) + ' checks passed.' if not failed else str(failed) + ' FAILED, ' + str(passed) + ' passed.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
