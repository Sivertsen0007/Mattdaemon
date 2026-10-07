#!/usr/bin/env python3
"""Real end-to-end test for Focus mode: the strip, the warm set, the switch.

No mocks and no fixtures. A real server, a real tmux on its own socket, real
shells, and a real browser driving the real page. The three things that can only
be proven this way are:

  the STRIP ranks what is waiting, and only after the debounce has let it,
  the WARM SET keeps the strip's sessions on the wire so a switch is free,
  and no warm pane ever squeezes a real session's tmux window.

That last one is the dangerous one. A pane with no box on screen has never been
fitted, so it still believes it is xterm's 80x24 - and the server sizes a session
to its SMALLEST reporting viewer. Get the warm panes wrong and the sessions you
are not looking at silently narrow to 80 columns. So the test watches every
window's width across the whole run rather than asserting it once at the end.

The socket is a scratch one (MTS_TMUX_SOCKET) precisely so this cannot happen to
the sessions you are actually working in.

Run: python3 test_focus.py
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

SOCKET = "mlsterm-focus-test"
os.environ["MTS_TMUX_SOCKET"] = SOCKET

passed = failed = 0


def _chromium():
    """Whatever Chromium this Mac already has, newest first; "" to let Playwright choose."""
    import glob
    pats = (os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/"
                               "chrome-headless-shell-mac-*/chrome-headless-shell"),
            os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/"
                               "chrome-mac*/Chromium.app/Contents/MacOS/Chromium"))
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return ""


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {label}")
    else:
        failed += 1
        print(f"  ✗ {label}")


def main():
    global failed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed - skipping (pip3 install playwright)")
        return 0

    workdir = tempfile.mkdtemp(prefix="mts-focustest-")
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

    def widths():
        out = tmux("list-sessions", "-F", "#{session_name} #{window_width}").stdout
        return dict(l.split() for l in out.splitlines() if " " in l)

    sids = {}
    try:
        for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo"):
            r = post("/api/terminal/start", {"name": name, "autostart": False})
            if not r.get("ok"):
                print(f"FAIL: could not start {name}: {r}")
                return 1
            sids[name] = r["session"]["id"]
        time.sleep(1.0)

        now = int(time.time())
        def stamp(name, state, need):
            s = "web-" + sids[name]
            tmux("set-option", "-t", s, "@webstate", f"{state}:{now}")
            if need:
                tmux("set-option", "-t", s, "@webneed", need)

        # Ranked on purpose: Bravo is the approval, so it must come FIRST even
        # though Alpha was stamped first and sits higher in every other listing.
        stamp("Alpha",   "attention", f"question|{now - 300}|which database?")
        stamp("Bravo",   "attention", f"approval|{now - 60}|rm -rf build")
        stamp("Charlie", "idle",      f"done|{now - 30}|the build is green")
        stamp("Delta",   "idle",      "")
        stamp("Echo",    "idle",      "")

        st = server._merged_states()
        check(st["needs"].get(sids["Bravo"], {}).get("kind") == "approval",
              "the server answers what a stamped session wants")
        check(sids["Delta"] not in st["needs"],
              "and says nothing about one that wants nothing")

        before = widths()

        with sync_playwright() as pw:
            # The Python client pins a browser build the Node install does not
            # have. Any Chromium speaks the same protocol, so the one that IS
            # installed is used rather than downloading a second copy.
            browser = pw.chromium.launch(executable_path=_chromium() or None)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_function("() => document.querySelectorAll('.sess').length >= 4",
                                   timeout=30000)
            check(not errors, f"the page runs clean{' - ' + errors[0] if errors else ''}")

            print("\nTHE STRIP RANKS WHAT IS WAITING")
            page.click("#btnFocus")
            check(page.eval_on_selector("body", "b => b.classList.contains('focus')"),
                  "Focus is a mode on the body, not a fifth layout")
            check(page.evaluate("() => document.querySelector('#term').dataset.layout") == "1",
                  "and it forces one pane")
            # Two polls, because an item has to survive a second look before it
            # is allowed on the strip.
            # tmux lists sessions in whatever order it likes, so WHICH session
            # the app opens on its own is not ours to assume. Park the stage on
            # one that wants nothing, and the whole ranking is then on the strip
            # with nothing pinned out of it.
            page.evaluate("sid => window.__mtsOpen(sid)", sids["Delta"])
            page.wait_for_function("() => window.__mtsQueue().length === 3", timeout=30000)
            check(page.evaluate("() => window.__mtsActive()") == sids["Delta"],
                  "the stage is parked on a session that wants nothing")
            check(page.eval_on_selector_all(".strip .strip-item.pinned", "e => e.length") == 0,
                  "so nothing is pinned, and Echo and Delta are not on the strip either")
            kinds = page.eval_on_selector_all(
                ".strip .strip-item .kind", "els => els.map(e => e.textContent)")
            check(kinds == ["approval", "question", "done"],
                  f"ranked by who is blocked, not by arrival ({kinds})")

            # And the pinning rule itself: go INTO the approval and it stops
            # being something the strip can send you to.
            page.evaluate("sid => window.__mtsOpen(sid)", sids["Bravo"])
            page.wait_for_function(
                "() => document.querySelectorAll('.strip .strip-item.pinned').length === 1",
                timeout=15000)
            check(page.eval_on_selector(".strip .strip-item.pinned .kind",
                                        "e => e.textContent") == "approval",
                  "the session you are in is pinned, never offered")
            check("approval" not in page.evaluate(
                      "() => window.__mtsQueue().map(i => i.kind)"),
                  "so the strip cannot drag you out of the reply you are reading")
            # Approve-from-the-strip is offered where "yes" is a button, and
            # nowhere else: a question wants typing, and an Enter into one would
            # answer it with whatever option happened to be highlighted.
            offers = page.eval_on_selector_all(
                ".strip .strip-item",
                "els => els.map(e => [e.querySelector('.kind').textContent,"
                " !!e.querySelector('.ok')])")
            check(all(has == (kind in ("approval", "plan")) for kind, has in offers),
                  "only an approval or a plan can be answered from the strip "
                  f"({offers})")
            page.evaluate("sid => window.__mtsOpen(sid)", sids["Delta"])
            page.wait_for_function("() => window.__mtsQueue().length === 3", timeout=15000)
            heads = page.eval_on_selector_all(
                ".strip .strip-item .head", "els => els.map(e => e.textContent)")
            check("rm -rf build" in heads,
                  "and each card names the thing, not its category")
            check(page.evaluate("() => document.querySelector('.strip-foot').textContent")
                  .find("waiting") > 0, "the footer counts the run")

            # A picture of the thing, when you ask for one. Assertions say the
            # strip is correct; only looking at it says whether it reads well.
            if os.environ.get("MTS_SHOT"):
                page.screenshot(path=os.environ["MTS_SHOT"])

            print("\nTHE STRIP IS THE WARM SET")
            page.wait_for_function("() => window.__mtsWarm().length >= 2", timeout=30000)
            warm = page.evaluate("() => window.__mtsWarm()")
            onwire = page.evaluate("() => window.__mtsStream()")
            check(all(w in onwire for w in warm),
                  "every warm session is on the wire before you click it")
            mounted = page.evaluate(
                "() => [...document.querySelectorAll('#warmHold > div')].map(d => d.dataset.sid)")
            check(all(w in mounted for w in warm), "and mounted, not merely subscribed")
            sizes = page.evaluate("""() => [...document.querySelectorAll('#warmHold > div')]
                .map(d => d.clientWidth + 'x' + d.clientHeight)""")
            check(bool(sizes) and all(not s.startswith("0x") and not s.endswith("x0")
                                      for s in sizes),
                  f"at a real size, never display:none ({sizes})")
            cols = page.evaluate("""() => {
                const out = {};
                for (const d of document.querySelectorAll('#warmHold > div'))
                    out[d.dataset.sid] = window.__mtsCols(d.dataset.sid);
                return out; }""")
            check(all(c > 80 for c in cols.values()),
                  f"and at the STAGE's geometry, not xterm's 80x24 default ({cols})")

            print("\nA WARM SWITCH COSTS NOTHING ON THE WIRE")

            target = page.evaluate("() => window.__mtsWarm()[0]")
            wire_before = page.evaluate("() => window.__mtsStream().slice().sort().join(',')")
            took_warm = page.evaluate("sid => window.__mtsOpen(sid)", target)
            page.wait_for_function("() => (window.__mtsSwitches || []).length > 0",
                                   timeout=10000)
            sw = page.evaluate("() => window.__mtsSwitches[window.__mtsSwitches.length - 1]")
            check(took_warm is True, "it took the warm path")
            check(sw["ms"] < 120, f"and landed inside a couple of frames ({sw['ms']}ms)")
            wire_after = page.evaluate("() => window.__mtsStream().slice().sort().join(',')")
            check(wire_before == wire_after,
                  "no sid changed on the stream, so nothing was replayed")
            check(page.evaluate("() => window.__mtsActive()") == target,
                  "and the session you clicked is on stage")

            # The baseline it has to beat. Delta is on nobody's strip, so it has
            # no pane, is not on the wire, and opening it is the old path: build
            # the pane, re-open the stream, let the server replay every session
            # on it. That is what a switch used to cost every single time.
            check(page.evaluate("sid => window.__mtsHasPane(sid)", sids["Echo"]) is False,
                  "Echo has no pane at all - it is on nobody's strip")
            took_cold = page.evaluate("sid => window.__mtsOpen(sid)", sids["Echo"])
            page.wait_for_function("sid => window.__mtsActive() === sid",
                                   arg=sids["Echo"], timeout=15000)
            cold_wire = page.evaluate("() => window.__mtsStream().slice().sort().join(',')")
            check(took_cold is False, "a session off the strip takes the cold path")
            # The number that matters is not the millisecond count - both paths
            # do their DOM work inside a frame - it is whether the wire moved.
            # A changed sid list is a re-opened stream, which is the server
            # replaying every pane on it and xterm resetting and reflowing each
            # one. That cost is paid after the frame this measures, and it is
            # exactly what the warm set exists to avoid.
            check(cold_wire != wire_after,
                  "and it DOES move the wire - which is the cost warm avoids")
            print(f"      (a warm switch did its work in {sw['ms']}ms)")

            print("\nNOTHING WAS SQUEEZED WHILE YOU WERE NOT LOOKING")
            # Watched over time rather than sampled once, which is the only way
            # this failure actually shows up: the size heartbeat speaks on its
            # own second-long clock, so a single snapshot taken straight after a
            # switch says nothing, and a pane that is going to drag a session
            # down to 80 does it a beat later, not immediately.
            #
            # A session nobody has ever opened keeps the 80x24 tmux made it at,
            # and that is correct - nothing is viewing it. The failure guarded
            # against here is the opposite: a session that HAS a viewer being
            # reported at xterm's untouched default.
            viewed = page.evaluate(
                "() => [...window.__mtsWarm(), window.__mtsActive()].filter(Boolean)")
            check(len(viewed) >= 2, f"(there were {len(viewed)} viewers to watch)")
            seen, thin, deadline = [], None, time.time() + 15
            while time.time() < deadline:
                w = widths()
                seen.append(w)
                thin = {"web-" + sid: w.get("web-" + sid) for sid in viewed
                        if int(w.get("web-" + sid, 0)) <= 80}
                if not thin:
                    break
                time.sleep(0.5)
            check(not thin, f"every session with a viewer reaches a real width ({thin})")
            # And stays there: a warm pane that reports its true size once and
            # then falls back to 80 would narrow the window behind your back.
            for _ in range(6):
                time.sleep(0.5)
                w = widths()
                seen.append(w)
            late = {k: v for w in seen[-6:] for k, v in w.items()
                    if k[len("web-"):] in viewed and int(v) <= 80}
            check(not late, f"and holds it for the rest of the run ({late})")
            shrunk = {k: (before[k], seen[-1][k]) for k in seen[-1]
                      if k in before and int(seen[-1][k]) < int(before[k])}
            check(not shrunk, f"no window got narrower than it started ({shrunk})")

            print("\nAND THE RUN DOES NOT LEAK")
            check(page.evaluate("() => window.__mtsPanes()") <= 8,
                  "mounted panes stay under the cap")
            # A focus run visits far more sessions than a wall ever did, and
            # `panes` was only ever torn down when a session died. Thirty
            # sessions visited is thirty live xterms, each holding a renderer and
            # a scrollback - a leak you only see after an hour.
            #
            # An hour is not a test, so this buys the same evidence with volume
            # instead of time: every session, hopped through many times over,
            # with the sweep on its real one-minute clock forced to run each lap.
            # If panes were never evicted this would end holding every one of
            # them; if the cap works it cannot exceed it whatever the order.
            every = page.evaluate("() => Object.keys(window.__mtsAllSids())")
            peak = 0
            for lap in range(6):
                for sid in every:
                    page.evaluate("sid => window.__mtsOpen(sid)", sid)
                page.evaluate("() => window.__mtsSweep()")
                peak = max(peak, page.evaluate("() => window.__mtsPanes()"))
            check(peak <= 8, f"after {6 * len(every)} switches it never passed the cap (peak {peak})")
            # The other half of the leak: the server's own side. A view is only
            # ever closed when its session dies or is stopped, so hopping must
            # not accumulate them, and none of them may be holding a dead reader.
            held = json.loads(urllib.request.urlopen(
                base + "/api/terminal/health", timeout=10).read()).get("held", {})
            panes_now = page.evaluate("() => window.__mtsPanes()")
            check(panes_now <= 8, f"and settles under it ({panes_now} panes)")
            check(held.get("views", 0) <= len(every),
                  f"the server holds no more views than there are sessions ({held})")
            check(held.get("dead_views", -1) == 0,
                  f"and none of them is a dead reader ({held})")
            page.click("#btnFocus")
            check(page.evaluate("() => document.querySelectorAll('#warmHold > div').length") == 0,
                  "leaving Focus puts every warm pane back")
            check(not errors, f"still clean{' - ' + errors[0] if errors else ''}")
            browser.close()
    finally:
        tmux("kill-server")
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{'All ' + str(passed) + ' checks passed.' if not failed else str(failed) + ' FAILED, ' + str(passed) + ' passed.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
