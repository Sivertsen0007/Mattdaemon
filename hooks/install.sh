#!/usr/bin/env bash
# Install the status-dot hooks into ~/.claude on this Mac.
#
# The dots are not drawn from the pane alone. A session that has stopped and a
# session that is waiting on a permission prompt can look identical on screen,
# and a /loop between iterations looks finished. These two hooks stamp what
# Claude actually DID onto the tmux session, and terminal_manager.py reads the
# stamps back:
#
#   webterm-state.sh   @webstate - the session's own account of itself, off
#                      SessionStart / Stop / PermissionRequest / Notification /
#                      PreToolUse / PostToolUse / SubagentStop / UserPromptSubmit.
#   webloop-stamp.py   @webloop  - a looping session's deadline, taken from the
#                      loop's own ScheduleWakeup / CronCreate call, so a dead
#                      loop goes honestly green instead of sitting yellow.
#
# Without them the app falls back to screen-scraping and the dots are wrong in
# exactly the cases they exist for. That is why they ship here and not only in
# one person's home directory.
#
# Safe to re-run: the copy is unconditional (the repo is the source of truth),
# the settings wiring is skipped per entry if an identical one is already there,
# and settings.json is backed up before it is touched.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"

mkdir -p "$DEST"
install -m 0755 "$SRC/webterm-state.sh" "$DEST/webterm-state.sh"
install -m 0755 "$SRC/webloop-stamp.py" "$DEST/webloop-stamp.py"
echo "==> Installed hooks into ${DEST/#$HOME/~}"

# The wiring. webloop-stamp guards on $TMUX because outside a tmux session there
# is nothing to stamp, and ends `; true` so a failed stamp never fails the hook
# and so never blocks Claude.
python3 - "$SETTINGS" "$DEST" <<'PY'
import json, io, os, shutil, sys, time

settings, dest = sys.argv[1], sys.argv[2]
state = f'"{dest}/webterm-state.sh"'
loop  = f'[ -n "$TMUX" ] && python3 "$HOME/.claude/hooks/webloop-stamp.py"'

# Every event webterm-state.sh knows how to answer, plus the two the loop stamp
# needs: PostToolUse to see the booking, Stop to roll the deadline forward.
WANT = {
    "SessionStart":      [f"{state} SessionStart"],
    "UserPromptSubmit":  [f"{state} UserPromptSubmit"],
    "PreToolUse":        [f"{state} PreToolUse"],
    "PostToolUse":       [f"{loop} PostToolUse >/dev/null 2>&1; true", f"{state} PostToolUse"],
    "PermissionRequest": [f"{state} PermissionRequest"],
    "Notification":      [f"{state} Notification"],
    "SubagentStop":      [f"{state} SubagentStop"],
    "Stop":              [f"{loop} Stop >/dev/null 2>&1; true", f"{state} Stop"],
}

d = {}
if os.path.exists(settings):
    with io.open(settings, encoding="utf-8") as f:
        d = json.load(f)          # a broken settings.json should stop us, loudly
    shutil.copy2(settings, f"{settings}.bak-{time.strftime('%Y%m%d-%H%M%S')}")

hooks = d.setdefault("hooks", {})
added = 0
for event, cmds in WANT.items():
    entries = hooks.setdefault(event, [])
    present = {h.get("command") for e in entries for h in e.get("hooks", [])}
    new = [c for c in cmds if c not in present]
    if new:
        entries.append({"matcher": "", "hooks": [{"type": "command", "command": c} for c in new]})
        added += len(new)

with io.open(settings, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2)
    f.write("\n")

print(f"==> Wired {added} hook command(s) into {settings.replace(os.path.expanduser('~'), '~')}"
      if added else "==> Hooks already wired, settings.json unchanged in substance")
PY

echo "==> Done. Restart Claude Code sessions for the hooks to take effect."
