"""The lifeboat: bring sessions back after the machine under them died.

tmux keeps a session alive through anything that happens to the app - a quit,
a reinstall, a crash of the viewer. It does not survive the machine: a power
cut on the Mac or a reboot of the box takes the tmux server, and every shell
in it, down with it. What does survive is the conversation. Claude writes each
message to disk as it goes, so all that is really lost is the link from "the
session called Website loop, in folder X, pane 2" to "conversation 7f3c...".

This module keeps that link on disk. A background thread writes a small
record of every live session every few seconds (atomically - a power cut in
the middle of a write leaves the previous record, never half of one). After a
restart, anything in the record that tmux no longer has is "lost", and
restore() starts it again under the SAME id, in the same folder, with
`claude --resume <id>`. The same id is the point: every sidebar folder, order
and pane placement the frontend keeps is keyed on it, so they all hold.

Lost vs closed. A session that disappears while this process is watching was
closed on purpose (stop, `exit`) and is forgotten. A session that is in the
record but was never seen alive by THIS process went down with the machine.
The one exception is the tmux server dying under a running app: several
sessions vanishing at once with the server gone is a crash, not a close.

Shared by the Mac app and the box. It only needs these from its terminal
manager (`tm`): _list_sessions_raw(), session_cwd(), start_session() taking
sid/resume/send_brief, and a `claude` field on each listed session.
"""

import json
import os
import threading
import time

INTERVAL = 4.0                     # seconds between record writes
KEEP_LOST = 7 * 24 * 3600          # a lost session is offered for a week

# What a session that was mid-work when it died gets typed into it - and NOT
# sent. Claude cannot know it was cut off, and carrying on blind could repeat
# a deploy or a send that had already gone out. You read it, and press Enter.
INTERRUPTED_NOTE = ("You were interrupted: the machine this session ran on went "
                    "down mid-turn. Check where you got to before carrying on, "
                    "and do not repeat anything that already went out.")

_lock = threading.Lock()
_record = {}            # sid -> entry
_seen_live = set()      # sids this process has seen alive
_path = None
_tm = None


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {e["sid"]: e for e in data.get("sessions", []) if e.get("sid")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save():
    """Write the record so that a power cut at any instant leaves a whole file."""
    tmp = _path + ".tmp"
    data = {"version": 1, "written": time.time(),
            "sessions": sorted(_record.values(), key=lambda e: e.get("created", 0))}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _path)


def _parse_claude(raw):
    """`<session_id>|<cwd>` from the hook -> (id, cwd)."""
    cid, _, cwd = (raw or "").partition("|")
    return cid.strip(), cwd.strip()


def _state_of(webstate):
    return (webstate or "").split(":", 1)[0]


def _headline(webneed):
    parts = (webneed or "").split("|", 2)
    return parts[2] if len(parts) == 3 else ""


def snapshot_once():
    """Fold one tmux listing into the record and write it. Safe to call often."""
    answered, live = _tm._list_sessions_raw()
    now = time.time()
    with _lock:
        live_ids = set()
        for s in live:
            sid = s["id"]
            live_ids.add(sid)
            _seen_live.add(sid)
            e = _record.get(sid, {"sid": sid, "created": s.get("created_at") or now})
            cid, ccwd = _parse_claude(s.get("claude"))
            e.update({
                "name": s.get("name") or e.get("name") or "",
                "agent": s.get("agent") or "",
                "worktree": s.get("worktree") or "",
                "worktree_name": s.get("worktree_name") or "",
                "state": _state_of(s.get("webstate")) or e.get("state", ""),
                "headline": _headline(s.get("webneed")) or e.get("headline", ""),
                "last_seen": now,
            })
            if cid:
                e["claude_id"], e["claude_cwd"] = cid, ccwd
            cwd = _tm.session_cwd(sid)
            if cwd:
                e["cwd"] = cwd
            e.pop("lost_at", None)
            _record[sid] = e

        if answered:
            vanished = [sid for sid in _record
                        if sid in _seen_live and sid not in live_ids
                        and not _record[sid].get("lost_at")]
            server_gone = not live
            if server_gone and len(vanished) >= 2:
                # The tmux server died under us: that is a crash, keep them all.
                for sid in vanished:
                    _record[sid]["lost_at"] = now
                    _seen_live.discard(sid)
            else:
                for sid in vanished:          # closed on purpose
                    _record.pop(sid, None)
                    _seen_live.discard(sid)

        # Never seen alive by this process and not in tmux: lost with the machine.
        for sid, e in list(_record.items()):
            if sid in live_ids or sid in _seen_live:
                continue
            e.setdefault("lost_at", now)
            if now - e["lost_at"] > KEEP_LOST:
                _record.pop(sid, None)
        _save()


def forget(sid):
    """A session closed through the app is never offered back."""
    with _lock:
        _record.pop(sid, None)
        _seen_live.discard(sid)
        if _path:
            _save()


def lost():
    """Sessions that went down with the machine, newest first."""
    with _lock:
        out = [dict(e) for e in _record.values() if e.get("lost_at")]
    out.sort(key=lambda e: e.get("last_seen", 0), reverse=True)
    for e in out:
        e["interrupted"] = e.get("state") == "working"
        e["resumable"] = bool(e.get("claude_id"))
    return out


def restore(sids=None):
    """Start lost sessions again, each under its old id. sids=None means all."""
    want = None if sids is None else set(sids)
    done, failed = [], []
    for e in lost():
        if want is not None and e["sid"] not in want:
            continue
        cwd = e.get("claude_cwd") or e.get("cwd") or None
        if cwd and not os.path.isdir(cwd):
            cwd = e.get("cwd") if e.get("cwd") and os.path.isdir(e["cwd"]) else None
        resume = e.get("claude_id") or None
        agent = e.get("agent") or ("claude" if resume else None)
        res = _tm.start_session(
            sid=e["sid"], name=e.get("name") or None, cwd=cwd,
            worktree=e.get("worktree") or None,
            worktree_name=e.get("worktree_name") or None,
            autostart=bool(agent), agent=agent, resume=resume,
            brief=INTERRUPTED_NOTE if (resume and e["interrupted"]) else None,
            send_brief=False)
        if res.get("ok"):
            with _lock:
                _record[e["sid"]].pop("lost_at", None)
                _seen_live.add(e["sid"])
            done.append(e["sid"])
        else:
            failed.append({"sid": e["sid"], "error": res.get("error", "")})
    with _lock:
        _save()
    return {"ok": not failed, "restored": done, "failed": failed}


def dismiss(sids):
    """Let lost sessions go for good."""
    with _lock:
        for sid in sids or []:
            _record.pop(sid, None)
        _save()
    return {"ok": True}


def start(tm, path):
    """Load the record and keep it written. Call once, at startup."""
    global _tm, _path
    _tm, _path = tm, path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock:
        _record.update(_load(path))

    def loop():
        while True:
            try:
                snapshot_once()
            except Exception:
                pass          # a lifeboat that sinks the app is worse than none
            time.sleep(INTERVAL)
    threading.Thread(target=loop, daemon=True, name="lifeboat").start()
