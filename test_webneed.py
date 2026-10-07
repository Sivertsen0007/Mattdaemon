#!/usr/bin/env python3
"""Real end-to-end test for @webneed: why a waiting session wants you.

No mocks. A real tmux server on its own socket, the real hook invoked the way
Claude Code invokes it, and terminal_manager's real reader on the other side.
The socket name carries "mlsterm" because the hook refuses to stamp anything
else, and the session is named web-* for the same reason - those two guards are
what keep the hook off every other Claude on the machine, so a test that
bypassed them would be testing a different program.

Run: python3 test_webneed.py
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import terminal_manager as tm

SOCKET = "mlsterm-webneed-test"
SESSION = "web-needtest"
HOOK = os.path.join(HERE, "hooks", "webterm-state.sh")

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {label}")
    else:
        failed += 1
        print(f"  ✗ {label}")


def tmux(*args, socket=SOCKET):
    return subprocess.run([tm.TMUX_BIN, "-L", socket, *args],
                          capture_output=True, text=True, timeout=10)


def fire(event, payload):
    """Invoke the hook exactly as Claude Code does: event as argv, JSON on stdin."""
    tmux_val = tmux("display-message", "-p", "#{socket_path}").stdout.strip() + ",0,0"
    env = dict(os.environ, TMUX=tmux_val)
    return subprocess.run(["bash", HOOK, event], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=10)


def opt(name):
    return tmux("show-options", "-v", "-q", name).stdout.strip()


def main():
    global failed
    if not os.path.exists(HOOK):
        print(f"hook not found: {HOOK}")
        return 1

    tmux("kill-server")
    r = tmux("new-session", "-d", "-s", SESSION, "-c", "/tmp", "/bin/bash")
    if r.returncode != 0:
        print(f"could not start a test tmux server: {r.stderr.strip()}")
        return 1

    try:
        print("\nTHE HOOK STAMPS WHAT ONLY IT KNOWS")
        fire("PermissionRequest", {"tool_name": "Bash",
                                   "tool_input": {"command": "rm -rf build"}})
        kind, _ts, head = tm._parse_webneed(opt("@webneed"))
        check(kind == "approval", "a Bash permission is an approval")
        check(head == "rm -rf build", "and the headline is the command, not the tool name")

        fire("PreToolUse", {"tool_name": "ExitPlanMode", "tool_input": {"plan": "x"}})
        kind, _ts, head = tm._parse_webneed(opt("@webneed"))
        check(kind == "plan", "ExitPlanMode is a plan, which the pane cannot tell")
        check(opt("@webstate").startswith("attention"),
              "and it stamps attention, not working - the turn has STOPPED")

        fire("PreToolUse", {"tool_name": "AskUserQuestion",
                            "tool_input": {"questions": [{"question": "Which DB?"}]}})
        kind, _ts, head = tm._parse_webneed(opt("@webneed"))
        check(kind == "question" and head == "Which DB?", "a question carries the question")

        fire("PermissionRequest", {"tool_name": "Edit",
                                   "tool_input": {"file_path": "/a/b/server.py"}})
        check(tm._parse_webneed(opt("@webneed"))[2] == "server.py",
              "an edit names the file, basename only")

        print("\nAND CLEARS IT THE MOMENT IT IS ANSWERED")
        fire("PostToolUse", {"tool_name": "Bash"})
        check(opt("@webneed") == "", "PostToolUse clears it - work is moving again")
        fire("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        check(opt("@webneed") != "", "(re-armed)")
        fire("Stop", {})
        check(opt("@webneed") == "",
              "Stop after a question clears it - nothing was built, so it is not news")
        fire("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        fire("PreToolUse", {"tool_name": "Grep", "tool_input": {"pattern": "x"}})
        check(opt("@webneed") == "", "an ordinary PreToolUse clears it too")

        print("\nA TURN THAT DID WORK AND STOPPED IS NEWS, NOT SILENCE")
        fire("PreToolUse", {"tool_name": "Grep", "tool_input": {"pattern": "x"}})
        check(opt("@webstate").startswith("working"), "(working)")
        fire("Stop", {})
        kind, ts, head = tm._parse_webneed(opt("@webneed"))
        check(kind == "done", "Stop after work stamps done - the build is ready for you")
        check(ts > 0, "and dates it, so the strip can age it out")
        check(opt("@webstate").startswith("idle"),
              "while the DOT still goes green - done is news, not a demand")
        fire("UserPromptSubmit", {})
        check(opt("@webneed") == "", "and your next prompt clears it")

        print("\nTHE FIELD SURVIVES HOSTILE HEADLINES")
        fire("PermissionRequest", {"tool_name": "Bash",
                                   "tool_input": {"command": "a|b\tc\nd"}})
        raw = opt("@webneed")
        check(raw.count("|") == 2, "pipes in the command cannot add a field")
        check("\t" not in raw, "and a tab cannot break the list-sessions format")
        fire("PermissionRequest", {"tool_name": "Bash",
                                   "tool_input": {"command": "x" * 500}})
        check(len(tm._parse_webneed(opt("@webneed"))[2]) <= 90, "a long one is trimmed")

        print("\nTHE READER PREFERS THE HOOK, AND FALLS BACK TO THE PANE")
        n = tm.session_need("s1", "plan|123|plan ready", "", "attention")
        check(n and n["kind"] == "plan" and n["source"] == "hook",
              "a stamped session is answered from the stamp")
        check(tm.session_need("s1", "plan|123|x", "", "idle") is None,
              "a GREEN session is never asked what it wants")
        check(tm.session_need("s1", "plan|123|x", "", "working") is None,
              "nor a yellow one")
        check(tm._parse_webneed("") == ("", 0.0, ""), "an absent stamp is not a need")
        check(tm._parse_webneed("approval") == ("", 0.0, ""), "nor a half-written one")
        check(tm._parse_webneed("approval|1|") == ("approval", 1.0, ""),
              "an empty headline still carries the kind and the time")

        print("\nDONE AGES OUT; A DEAD TURN SPEAKS UP")
        now = time.time()
        n = tm.session_need("s1", f"done|{now:.0f}|build green", "", "idle")
        check(n and n["kind"] == "done" and n["headline"] == "build green",
              "a fresh done reaches the strip even though the dot is green")
        check(tm.session_need("s1", f"done|{now - 3600:.0f}|old news", "", "idle") is None,
              "an hour-old one has stopped being news")
        dead = tm.session_need("s1", "", "", "idle",
                               webstate=f"working:{now - 600:.0f}")
        check(dead is not None and dead["kind"] == "stuck",
              "a working stamp that went stale while the dot fell back to idle is stuck")
        check(tm.session_need("s1", "", "", "idle",
                              webstate=f"working:{now:.0f}") is None,
              "but a fresh working stamp on an idle dot is just a race, not a death")

        print("\nall_states ANSWERS ONLY FOR THE WAITING")
        real_socket = tm.TMUX_SOCKET
        tm.TMUX_SOCKET = SOCKET
        try:
            fire("PermissionRequest", {"tool_name": "Bash",
                                       "tool_input": {"command": "deploy prod"}})
            st = tm.all_states()
            check("needs" in st, "the poll carries a needs map")
            sid = SESSION[len("web-"):]
            got = st["needs"].get(sid)
            if st["states"].get(sid) == "attention":
                check(bool(got), "the waiting session is in it")
                check(got and got["headline"] == "deploy prod",
                      "with the headline the hook stamped")
            else:
                # A bare bash prompt is idle, which is the correct reading of the
                # pane; the stamp alone must not turn a row red.
                check(got is None,
                      "a stamp on a session the dots call idle adds no need "
                      f"(state={st['states'].get(sid)!r})")
            check(all(st["states"].get(k) in ("attention", "idle") for k in st["needs"]),
                  "nothing working or offline ever reaches the strip")
        finally:
            tm.TMUX_SOCKET = real_socket
    finally:
        tmux("kill-server")

    print(f"\n{'All ' + str(passed) + ' checks passed.' if not failed else str(failed) + ' FAILED, ' + str(passed) + ' passed.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
