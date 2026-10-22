#!/usr/bin/env python3
"""Real end-to-end test for Claude accounts in Settings.

A scratch HOME (so the real ~/.claude is never touched), a fake `claude` that
answers `auth status` per CLAUDE_CONFIG_DIR and echoes a login instead of
opening a browser, real shells on a scratch tmux socket, and a real browser
driving the real Settings panel. It proves:

  an account is a folder with its own login and symlinks to the shared set-up,
  names are checked (empty, duplicate, more than five),
  "+ New" and the worktree picker are built from the accounts, not hard-coded,
  adding one from Settings opens a session that signs in THAT account,
  a session started on it runs Claude with that account's CLAUDE_CONFIG_DIR,
  rename shows everywhere, and remove deletes the folder but never the shared
  files it linked to.

Run: python3 test_accounts.py
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

from test_focus import _chromium  # noqa: E402  (sets its own socket; overridden below)

CHROME = _chromium()                           # found under the REAL home
SOCKET = "mlsterm-accounts-test"
os.environ["MTS_TMUX_SOCKET"] = SOCKET
TMP = tempfile.mkdtemp(prefix="mts-accounts-")
REAL_HOME = os.environ["HOME"]
os.environ["HOME"] = TMP                       # every ~ below is the scratch one
os.environ["MTS_CONFIG"] = os.path.join(TMP, "cfg", "mts-config.json")
os.environ["TERMINAL_HOME"] = TMP

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
        print("playwright is not installed - skipping")
        return 0

    main_dir = os.path.join(TMP, ".claude")
    os.makedirs(os.path.join(main_dir, "hooks"))
    os.makedirs(os.path.join(main_dir, "projects"))
    with open(os.path.join(main_dir, "settings.json"), "w") as f:
        f.write('{"shared": true}')

    fake = os.path.join(TMP, "fake-claude")
    with open(fake, "w") as f:
        f.write("""#!/bin/bash
if [ "$2" = "status" ]; then
  case "$CLAUDE_CONFIG_DIR" in
    *-dt) echo '{"loggedIn":true,"email":"dt@example.com","subscriptionType":"team","authMethod":"claude.ai"}' ;;
    "")   echo '{"loggedIn":true,"email":"ml@example.com","subscriptionType":"max","authMethod":"claude.ai"}' ;;
    *)    echo '{"loggedIn":false}' ;;
  esac
  exit 0
fi
echo "FAKE-CLAUDE dir=${CLAUDE_CONFIG_DIR:-main} args=$*"
""")
    os.chmod(fake, 0o755)

    import server
    st = server.setup
    st.claude_binary = lambda: fake
    st.save_config({"setupComplete": True, "home": TMP, "mainAccountLabel": "ML"})
    tm = server._import_manager()
    tm.START_DIR = TMP
    if tm.TMUX_SOCKET != SOCKET:
        print("FAIL: refusing to run against the real socket")
        return 1
    # Sessions type `claude`; make that the fake too.
    tm.AGENTS["claude"] = dict(tm.AGENTS["claude"], cmd=fake)

    try:
        print("ACCOUNTS ARE FOLDERS")
        res = st.add_account("DT")
        dt = os.path.join(TMP, ".claude-dt")
        check(res["ok"] and os.path.isdir(dt), "adding DT creates ~/.claude-dt")
        check(os.path.islink(os.path.join(dt, "settings.json"))
              and os.path.realpath(os.path.join(dt, "settings.json"))
              == os.path.realpath(os.path.join(main_dir, "settings.json")),
              "settings are linked to the main ones, not copied")
        check(os.path.islink(os.path.join(dt, "projects")), "and so is memory (projects)")
        check(not os.path.exists(os.path.join(dt, ".claude.json")), "its login starts empty")
        check(not st.add_account("")["ok"], "an empty name is refused")
        check(not st.add_account("dt")["ok"], "a duplicate name is refused")
        for n in ("A", "B", "C"):
            st.add_account(n)
        r = st.add_account("Sixth")
        check(not r["ok"] and "5" in r["error"], "a sixth account is refused")
        for n in ("a", "b", "c"):
            st.remove_account(n)
        spec = tm.agent_spec("claude-dt")
        check(spec and "CLAUDE_CONFIG_DIR=" + dt in spec["cmd"],
              "claude-dt runs Claude with the DT folder")
        check(tm.agent_spec("claude-nope") is None, "an unknown account is an unknown agent")

        httpd = server._make_server(0)
        host, port = httpd.server_address
        base = f"http://{host}:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def get(path):
            with urllib.request.urlopen(base + path, timeout=15) as r:
                return json.loads(r.read())

        accs = {a["label"]: a for a in get("/api/setup/state")["accounts"]}
        check(accs.get("ML", {}).get("email") == "ml@example.com"
              and accs.get("DT", {}).get("email") == "dt@example.com",
              "each account reports its own login")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME or None)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelectorAll('#agentItems button').length >= 3",
                timeout=30000)
            check(not errors, f"the page runs clean{' - ' + errors[0] if errors else ''}")

            print("\n+ NEW IS BUILT FROM THE ACCOUNTS")
            items = page.eval_on_selector_all("#agentItems button",
                                              "bs => bs.map(b => b.textContent.trim())")
            check(any("Claude (ML)" in t for t in items) and any("Claude (DT)" in t for t in items),
                  "the menu offers Claude (ML) and Claude (DT)")
            opts = page.eval_on_selector_all("#wtAgent option", "os => os.map(o => o.value)")
            check("claude-dt" in opts, "and so does the worktree picker")

            print("\nA SESSION ON DT USES DT")
            page.click("#btnNew")
            page.click("#agentItems button[data-agent='claude-dt']")
            time.sleep(2.5)
            panes = [tm._tmux("capture-pane", "-p", "-t", tm._sname(s["id"])).stdout
                     for s in tm._tmux_sessions()]
            check(any("FAKE-CLAUDE dir=" + dt in p for p in panes),
                  "the session runs Claude with CLAUDE_CONFIG_DIR = the DT folder")

            print("\nADD AN ACCOUNT FROM SETTINGS")
            page.click("#btnSettings")
            page.wait_for_selector("#setAccounts .row")
            rows = page.eval_on_selector_all("#setAccounts .rname", "ns => ns.map(n => n.textContent)")
            check(rows[:2] == ["ML  (main)", "DT"], "Settings lists ML (main) and DT")
            page.click("#setAccAddBtn")
            page.fill("#setAccName", "Acme")
            page.click("#setAccCreate")
            end = time.time() + 10
            while time.time() < end and not os.path.isdir(os.path.join(TMP, ".claude-acme")):
                time.sleep(0.2)
            check(os.path.isdir(os.path.join(TMP, ".claude-acme")), "Add creates ~/.claude-acme")
            ok = False
            for _ in range(20):
                time.sleep(0.5)
                for s in tm._tmux_sessions():
                    if s["name"] == "Sign in: Acme":
                        p = tm._tmux("capture-pane", "-p", "-t", tm._sname(s["id"])).stdout
                        ok = "dir=" + os.path.join(TMP, ".claude-acme") in p and "auth login" in p
                if ok:
                    break
            check(ok, "and opens a session signing in THAT account")
            page.wait_for_function(
                "() => [...document.querySelectorAll('#agentItems button')]"
                ".some(b => b.textContent.includes('Claude (Acme)'))", timeout=10000)
            check(True, "Acme is in + New straight away")

            print("\nRENAME AND REMOVE")
            st.rename_account("acme", "Acme Ltd")
            page.click("#btnSettings")
            page.wait_for_function(
                "() => [...document.querySelectorAll('#setAccounts .rname')]"
                ".some(n => n.textContent === 'Acme Ltd')", timeout=10000)
            check(True, "a rename shows in Settings")
            row = page.locator("#setAccounts .row", has=page.locator(".rname", has_text="Acme Ltd"))
            row.locator("button", has_text="Remove").click()
            check(os.path.isdir(os.path.join(TMP, ".claude-acme")), "one click only arms Remove")
            row.locator("button", has_text="Click again").click()
            page.wait_for_function(
                "() => ![...document.querySelectorAll('#setAccounts .rname')]"
                ".some(n => n.textContent === 'Acme Ltd')", timeout=10000)
            check(not os.path.exists(os.path.join(TMP, ".claude-acme")),
                  "the second click deletes its folder")
            check(os.path.isfile(os.path.join(main_dir, "settings.json"))
                  and os.path.isdir(os.path.join(main_dir, "hooks")),
                  "and the shared files it linked to are untouched")
            check(not errors, f"still clean{' - ' + errors[0] if errors else ''}")
            browser.close()
    finally:
        tm._tmux("kill-server")
        os.environ["HOME"] = REAL_HOME
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\nAll {passed} checks passed." if not failed
          else f"\n{failed} of {passed + failed} checks FAILED.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
