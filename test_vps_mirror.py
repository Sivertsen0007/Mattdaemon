#!/usr/bin/env python3
"""Test for "continue on this Mac" - the half of the lifeboat for a box that is DOWN.

The box is faked (its snapshot and its file endpoint), everything else is real:
a scratch HOME so the copied conversation lands in a throwaway ~/.claude, and
a real tmux session on a scratch socket for the continued session. It proves:

  a live box session's conversation is copied into the matching Mac project
  folder (path_map applied, Claude's folder naming),
  nothing is offered while the box answers,
  once the box has been silent long enough it is offered,
  continuing starts a local session with `--resume <same id>` in the mapped
  folder, and tells the page which old id became which.

Run: python3 test_vps_mirror.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="mts-mirror-")
os.environ["HOME"] = TMP                       # copies land in a throwaway ~/.claude
os.environ["MTS_CONFIG"] = os.path.join(TMP, "cfg", "mts-config.json")
os.environ["MTS_TMUX_SOCKET"] = "mlsterm-mirror-test"
os.makedirs(os.path.join(TMP, "cfg"))

import server  # noqa: E402

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
    tm = server._import_manager()
    import lifeboat
    server.lifeboat = lifeboat
    # Nothing real is launched: "claude" echoes what it was asked to run.
    tm.AGENTS["claude"] = dict(tm.AGENTS["claude"], cmd="echo FAKECLAUDE")

    local_repo = os.path.join(TMP, "code", "AI-Hub")
    os.makedirs(local_repo)
    server.VPS_BASE, server.VPS_TOKEN = "http://box.invalid", "t"
    server.VPS_PATH_MAP.clear()
    server.VPS_PATH_MAP[local_repo] = "/home/aihub/AI-Hub"

    asked = []
    convo = b'{"type":"user","message":{"content":"hello"}}\n'
    server._vps_get_bytes = lambda path, timeout=None: (asked.append(path), convo)[1]
    with server._vps_snap_lock:
        server._vps_snap.update({"ts": time.time(), "sessions": [{
            "id": "vps:abc123", "realid": "abc123", "name": "Box build",
            "agent": "claude", "claude": "conv-777|/home/aihub/AI-Hub"}]})

    try:
        print("copy")
        server._mirror_once()
        want_remote = "/home/aihub/.claude/projects/-home-aihub-AI-Hub/conv-777.jsonl"
        check(any(want_remote in __import__("urllib.parse").parse.unquote(p) for p in asked),
              "the box's transcript is fetched from its Claude project folder")
        dest = os.path.join(TMP, ".claude", "projects",
                            server._project_slug(local_repo), "conv-777.jsonl")
        check(os.path.isfile(dest) and open(dest, "rb").read() == convo,
              "and written into the matching Mac project folder")
        check(server._vps_continuable() == [], "nothing is offered while the box answers")

        print("box goes down")
        with server._vps_snap_lock:
            server._vps_snap["ts"] = time.time() - server._VPS_DOWN_AFTER - 5
        rows = server._vps_continuable()
        check([r["sid"] for r in rows] == ["vps:abc123"], "once silent, the box session is offered")

        print("continue here")
        res = server.lifeboat_restore(["vps:abc123"])
        new = res["moved"].get("vps:abc123")
        check(res["ok"] and new and not new.startswith("vps:"),
              "it becomes a local session, and the page is told the new id")
        pane = lambda: tm._tmux("capture-pane", "-p", "-t", tm._sname(new)).stdout or ""
        end = time.time() + 10
        while time.time() < end and "FAKECLAUDE --resume conv-777" not in pane():
            time.sleep(0.25)
        check("FAKECLAUDE --resume conv-777" in pane(), "running --resume with the same conversation")
        check(os.path.realpath(tm.session_cwd(new) or "") == os.path.realpath(local_repo),
              "in the mapped Mac folder")
        check(server._vps_continuable() == [], "and it is not offered twice")
    finally:
        tm._tmux("kill-server")
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\nAll {passed} checks passed." if not failed
          else f"\n{failed} of {passed + failed} checks FAILED.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
