#!/usr/bin/env bash
# Mattdaemon status hook - the Mac half of the web-terminal status dots.
#
# Stamps @webstate on the tmux session so the app's poll can colour the dot from
# what Claude/Codex actually DID, instead of only from what its screen happens to
# say. The box has had this since 2026-08-26 (~/.claude/hooks/webterm-state.sh on
# the VPS); the Mac never got it, so local sessions ran on screen-scraping alone -
# which is why the VPS dots were right and the Mac's were not.
#
# Differences from the box's copy, on purpose:
#   - socket scope is `mlsterm` (Mattdaemon's tmux server), not `aihub-web`
#   - no Telegram push. The box owns pushes, and they are muted anyway; here the
#     dot IS the notification, since the app is on screen.
#   - the @webloop stamp is NOT delegated from here. On the Mac webloop-stamp.py
#     is already wired into settings.json on its own (PostToolUse + Stop), and
#     calling it twice per event would just burn a python start.
#
# Best-effort by design: never blocks Claude, always exits 0.
# Invoked from ~/.claude/settings.json as: webterm-state.sh <EventName>
EVENT="${1:-}"
JSON="$(cat 2>/dev/null)"

# Only inside a Mattdaemon tmux session. $TMUX is "<socket path>,<pid>,<idx>",
# and Mattdaemon's server is -L mlsterm, so its path carries that name. Any other
# Claude on this Mac - a plain terminal, the VS Code one, a farm worker - has no
# $TMUX or a different socket, and falls straight out here.
case "${TMUX:-}" in
  *mlsterm*) ;;
  *) exit 0 ;;
esac

TMUX_BIN="$(command -v tmux 2>/dev/null)"
[ -z "$TMUX_BIN" ] && TMUX_BIN="$HOME/.local/bin/tmux"
[ -x "$TMUX_BIN" ] || exit 0
tmuxb() { "$TMUX_BIN" "$@" 2>/dev/null; }

# Only the app's own sessions (web-<sid>). Anything else sharing the socket must
# not paint a dot that belongs to no row.
case "$(tmuxb display-message -p '#{session_name}')" in
  web-*) ;;
  *) exit 0 ;;
esac

# kind:epoch - the epoch is what lets classify_session age a `working` stamp out,
# so a session whose agent died mid-turn (never firing Stop) cannot sit yellow.
stamp() { tmuxb set-option "@webstate" "$1:$(date +%s)"; }

msg="$(printf '%s' "$JSON" | jq -r '.message // empty' 2>/dev/null)"

case "$EVENT" in
  PermissionRequest)
    stamp attention ;;
  Notification)
    # "Claude is waiting for your input" is just an idle prompt, not a decision
    # to make - leave it green (Stop already stamped idle). Everything else is
    # something that wants you.
    case "$msg" in
      *waiting*input*|*"waiting for your"*) : ;;
      *) stamp attention ;;
    esac ;;
  Stop)
    # Turn finished - whatever was asked has been answered.
    stamp idle ;;
  SubagentStop)
    stamp idle ;;
  UserPromptSubmit|PreToolUse|PostToolUse)
    # Work is moving again, so any prompt got answered.
    stamp working ;;
  SessionStart)
    stamp idle ;;
esac
exit 0
