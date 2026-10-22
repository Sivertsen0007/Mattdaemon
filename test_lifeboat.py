#!/usr/bin/env python3
"""Real end-to-end test for the lifeboat.

Real shells on a scratch tmux socket, the real status hook, and a real "power
cut": the tmux server is killed and the lifeboat module reloaded from disk, as a
new app process would. It proves:

  the hook stamps which conversation a session is having (@webclaude),
  the record survives the tmux server dying,
  every lost session comes back under its OLD id, name and folder, running
  `<agent> --resume <conversation id>`,
  a session that died mid-work gets the note typed in but NOT sent,
  a session closed on purpose is never offered back.

Run: python3 test_lifeboat.py
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Must contain "mlsterm": the hook only acts inside Mattdaemon's own socket.
SOCKET = "mlsterm-lifeboat-test"
os.environ["MTS_TMUX_SOCKET"] = SOCKET
HOOK = os.path.expanduser("~/.claude/hooks/webterm-state.sh")

import terminal_manager as tm  # noqa: E402
import lifeboat  # noqa: E402

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {label}")
    else:
        failed += 1
        print(f"  ✗ {label}")


def pane(sid):
    return tm._tmux("capture-pane", "-p", "-t", tm._sname(sid)).stdout or ""


def opt(sid, name):
    return (tm._tmux("show-options", "-v", "-t", tm._sname(sid), name).stdout or "").strip()


def wait_for(fn, secs=8.0):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.25)
    return False


# Stand-ins for Claude, so nothing real is launched. The name starts with
# "claude", which is what makes start_session add `--resume <id>`.
tm.AGENTS["claude-fake"] = {
    "name": "claude-fake", "label": "fake", "cmd": "echo FAKECLAUDE",
    "brief_as_arg": False, "trust": ("never-shown-trust",)}
# One that also draws Claude's ready caret, so the interrupted note gets typed.
tm.AGENTS["claude-ready"] = {
    "name": "claude-ready", "label": "fake", "cmd": "printf '\\n\\u276f \\n? for shortcuts\\n'; echo",
    "brief_as_arg": False, "trust": ("never-shown-trust",)}


def main():
    tmp = tempfile.mkdtemp(prefix="mts-lifeboat-")
    record = os.path.join(tmp, "sessions.json")
    work = os.path.join(tmp, "proj")
    os.makedirs(work)
    tm._tmux("kill-server")
    try:
        print("hook")
        a = tm.start_session("Website loop", cwd=work)["session"]["id"]
        payload = json.dumps({"session_id": "conv-aaa", "cwd": work})
        # A login shell can take a moment; keys typed before its prompt are lost.
        wait_for(lambda: pane(a).rstrip().endswith("$"), 15)
        tm._tmux("send-keys", "-t", tm._sname(a),
                 "echo '%s' | %s SessionStart" % (payload, HOOK), "Enter")
        check(wait_for(lambda: opt(a, "@webclaude") == "conv-aaa|" + work),
              "hook stamps @webclaude = id|cwd")

        print("record")
        b = tm.start_session("Deploy work", cwd=work)["session"]["id"]
        c = tm.start_session("Throwaway", cwd=work)["session"]["id"]
        for sid, agent, conv in ((a, "claude-fake", "conv-aaa"),
                                 (b, "claude-ready", "conv-bbb")):
            tm._tmux("set-option", "-t", tm._sname(sid), "@webagent", agent)
            tm._tmux("set-option", "-t", tm._sname(sid), "@webclaude", conv + "|" + work)
        tm._tmux("set-option", "-t", tm._sname(b), "@webstate",
                 "working:%d" % int(time.time()))
        lifeboat._tm, lifeboat._path = tm, record
        lifeboat.snapshot_once()
        rec = json.load(open(record))["sessions"]
        check(len(rec) == 3, "all three sessions are in the record")

        print("closed on purpose")
        tm.stop_session(c)
        lifeboat.snapshot_once()
        check(c not in {e["sid"] for e in json.load(open(record))["sessions"]},
              "a session closed while watched is forgotten")

        print("power cut")
        tm._tmux("kill-server")
        importlib.reload(lifeboat)                  # a new app process
        lifeboat._tm, lifeboat._path = tm, record
        lifeboat._record.update(lifeboat._load(record))
        lifeboat.snapshot_once()
        lost = {e["sid"]: e for e in lifeboat.lost()}
        check(set(lost) == {a, b}, "both surviving sessions are reported lost")
        check(lost.get(b, {}).get("interrupted") is True, "the busy one is marked interrupted")
        check(lost.get(a, {}).get("interrupted") is False, "the idle one is not")

        print("restore")
        res = lifeboat.restore()
        check(res["ok"] and set(res["restored"]) == {a, b}, "restore reports both back")
        check(tm._session_exists(a) and tm._session_exists(b), "both come back under their OLD ids")
        names = {s["id"]: s["name"] for s in tm._tmux_sessions()}
        check(names.get(a) == "Website loop" and names.get(b) == "Deploy work",
              "names are kept")
        check(wait_for(lambda: "FAKECLAUDE --resume conv-aaa" in pane(a)),
              "the agent is started with --resume <conversation id>")
        check(os.path.realpath(tm.session_cwd(a) or "") == os.path.realpath(work),
              "it opens in the same folder")
        check(wait_for(lambda: "You were interrupted" in pane(b), 12),
              "the interrupted session gets the note typed in")
        time.sleep(1.5)
        lines = [l for l in pane(b).splitlines() if l.strip()]
        # Unsent = still sitting on the input line: the last thing on screen,
        # with no shell answer ("command not found") or new prompt after it.
        check(bool(lines) and "already went out." in lines[-1]
              and "command not found" not in pane(b),
              "the note is not sent")
        lifeboat.snapshot_once()
        check(lifeboat.lost() == [], "nothing is offered back after the restore")
    finally:
        tm._tmux("kill-server")
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nAll {passed} checks passed." if not failed
          else f"\n{failed} of {passed + failed} checks FAILED.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
