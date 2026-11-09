#!/usr/bin/env python3
"""Scrolling a pane through tmux's history, on a real tmux of its own.

The bug this guards: scroll up, scroll back down to the bottom (copy-mode -e
leaves the mode by itself there), then scroll up again. The app still thought
the pane was scrolled up, skipped entering copy-mode, and tmux answered
"not in a mode" - so every scroll up failed until a scroll down happened to
reset it. With a trackpad that made scrolling feel all but broken, worst in
pop-out windows where long answers get read.

Run: python3 test_scroll.py
"""
import os
import subprocess
import sys
import time

os.environ["MTS_TMUX_SOCKET"] = "mtd-scrolltest-%d" % os.getpid()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import terminal_manager as tm  # noqa: E402

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print("  ✓ " + label)
    else:
        failed += 1
        print("  ✗ " + label)


def main():
    if not tm._tmux_available():
        print("SKIP: no tmux")
        return 0
    sid = "scrolltest"
    name = tm._sname(sid)
    tm._tmux("new-session", "-d", "-s", name, "-x", "80", "-y", "24",
             "sh -c 'seq 1 500; sleep 60'")
    time.sleep(0.5)
    in_mode = lambda: tm._tmux("display", "-p", "-t", name,
                               "#{pane_in_mode}").stdout.strip() == "1"
    try:
        print("\nSCROLLING")
        r = tm.scroll_session(sid, -5)
        check(r["ok"] and in_mode(), "scroll up enters the history")
        r = tm.scroll_session(sid, 3)
        check(r["ok"] and in_mode(), "a little down stays in it")
        tm.scroll_session(sid, 500)
        check(not in_mode(), "all the way down is live again")
        r = tm.scroll_session(sid, -5)
        check(r["ok"] and in_mode(), "scrolling up again works straight away (%s)" % r.get("error"))
        r = tm.scroll_session(sid, -5)
        check(r["ok"] and in_mode(), "and keeps working")
        tm.scroll_session(sid, 500)
        tm.scroll_session(sid, 500)
        r = tm.scroll_session(sid, -5)
        check(r["ok"] and in_mode(), "even after extra scrolls past the bottom")
        tm._leave_copy_mode(sid)
        check(not in_mode(), "typing leaves the history")
    finally:
        tm._tmux("kill-server")

    print("\n%d of %d checks passed." % (passed, passed + failed) if not failed
          else "\n%d of %d checks FAILED." % (failed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
