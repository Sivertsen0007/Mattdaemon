#!/usr/bin/env python3
"""Keep a looping session's dot yellow.

A /loop is quiet between its iterations. The pane sits at a prompt, the Stop
hook stamps `idle`, and the dashboard paints the session green - which says
"done, waiting for you" about a session that is going to carry on by itself. So
you go and look, and there was nothing to do.

`term-loop` has always been able to say otherwise, but only if the agent
remembers to call it every iteration. This does the same thing from the hook
side, off what the loop itself does:

  ScheduleWakeup   a self-paced /loop books its next tick and says when. That
                   delay IS the answer: armed until then, plus a margin.
  CronCreate       an interval /loop books a cron entry instead. The expression
                   is remembered for this session so the stamp can be rolled
                   forward on every tick, and the next fire time is worked out
                   from it.
  CronDelete       the loop is over; forget it and go honestly idle.

The stamp is a DEADLINE, never a flag: a loop that dies, crashes or is killed
stops refreshing it and the session goes green on its own. Nothing here can
strand a dot yellow.

Invoked from webterm-state.sh with the hook's event name and its JSON on stdin.
Best-effort by design: it never blocks Claude and always exits 0.
"""
import json
import os
import subprocess
import sys
import time

# How far past the next wake-up the stamp stays armed. Long enough that a tick
# which starts late does not blink green first, short enough that a loop you
# stopped is not still claiming to be running minutes later.
MARGIN = 180
STATE_DIR = os.path.expanduser("~/.claude/webloop")
# A remembered cron entry older than this is treated as forgotten: sessions are
# long-lived and a stale file must not keep a dot yellow for ever.
STATE_MAX_AGE = 7 * 86400


def tmux(*args):
    try:
        subprocess.run(["tmux", *args], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def session_name():
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "#{session_name}"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def arm(deadline, label="loop"):
    tmux("set-option", "@webloop", "%d:%s" % (int(deadline), label))


def disarm():
    tmux("set-option", "-u", "@webloop")


def state_path(sess):
    return os.path.join(STATE_DIR, "%s.json" % sess.replace("/", "_"))


def remember(sess, cron):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(state_path(sess), "w") as fh:
            json.dump({"cron": cron, "at": time.time()}, fh)
    except OSError:
        pass


def recall(sess):
    try:
        with open(state_path(sess)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    if time.time() - float(data.get("at") or 0) > STATE_MAX_AGE:
        return ""
    return str(data.get("cron") or "")


def forget(sess):
    try:
        os.remove(state_path(sess))
    except OSError:
        pass


def _field_matches(spec, value, lo, hi):
    """One cron field against one value. Handles *, lists, ranges and steps."""
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            try:
                step = int(raw_step)
            except ValueError:
                return False
            if step < 1:
                return False
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                return False
        else:
            try:
                start = end = int(part)
            except ValueError:
                return False
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def next_fire(cron, now=None):
    """When a 5-field cron expression next fires, or 0 if it does not soon.

    Walked minute by minute rather than solved, because the only expressions
    that reach here are a loop's own interval and a day of stepping costs a
    couple of thousand cheap comparisons once per tick.
    """
    fields = cron.split()
    if len(fields) != 5:
        return 0.0
    minute, hour, dom, month, dow = fields
    now = now or time.time()
    t = int(now // 60) * 60 + 60          # the next whole minute
    for _ in range(2 * 24 * 60):          # two days is longer than any loop
        lt = time.localtime(t)
        # cron's day-of-week is 0-6 with Sunday 0; python's is Monday 0.
        wd = (lt.tm_wday + 1) % 7
        # cron's day rule: with both day fields restricted they are ORed, and
        # with one of them a wildcard only the other one is asked.
        any_dom = dom.strip() in ("*", "?")
        any_dow = dow.strip() in ("*", "?")
        if any_dom and any_dow:
            day_ok = True
        elif any_dow:
            day_ok = _field_matches(dom, lt.tm_mday, 1, 31)
        elif any_dom:
            day_ok = _field_matches(dow, wd, 0, 6)
        else:
            day_ok = (_field_matches(dom, lt.tm_mday, 1, 31)
                      or _field_matches(dow, wd, 0, 6))
        if (day_ok
                and _field_matches(minute, lt.tm_min, 0, 59)
                and _field_matches(hour, lt.tm_hour, 0, 23)
                and _field_matches(month, lt.tm_mon, 1, 12)):
            return float(t)
        t += 60
    return 0.0


def main():
    if not os.environ.get("TMUX"):
        return
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    sess = session_name()
    if not sess:
        return

    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input") or {}

    if event == "PostToolUse" and tool == "ScheduleWakeup":
        if args.get("stop"):
            forget(sess)
            disarm()
            return
        try:
            delay = float(args.get("delaySeconds") or 0)
        except (TypeError, ValueError):
            delay = 0.0
        if delay > 0:
            arm(time.time() + delay + MARGIN)
        return

    if event == "PostToolUse" and tool == "CronCreate":
        cron = str(args.get("cron") or "")
        when = next_fire(cron)
        if when:
            if args.get("recurring", True):
                remember(sess, cron)
            arm(when + MARGIN)
        return

    if event == "PostToolUse" and tool == "CronDelete":
        forget(sess)
        disarm()
        return

    # Every tick of an interval loop comes in as a prompt and ends with a Stop.
    # Rolling the stamp forward there is what keeps the gap between ticks yellow
    # without the loop having to say anything.
    if event in ("UserPromptSubmit", "Stop"):
        cron = recall(sess)
        if not cron:
            return
        when = next_fire(cron)
        if when:
            arm(when + MARGIN)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass    # a status dot is never worth failing a hook over
