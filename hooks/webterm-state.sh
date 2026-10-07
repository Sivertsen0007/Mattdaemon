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

# @webneed is WHY this session wants you: `kind|epoch|headline`.
#
# The pane can tell you a box is open. Only the hook knows WHICH box, because it
# is handed the tool name and its input: a plan awaiting your go-ahead and a
# Bash permission draw nearly the same screen, and the headline the pane can
# scrape is whatever words happened to be above the options. So this is the
# precise half, and terminal_manager's _pane_reason stays as the floor for a
# session that was already waiting before the hook was installed.
#
# Pipes and tabs are stripped: the field is pipe-delimited, and a tab would
# break the tmux list-sessions format that carries it.
need() {
  _h="$(printf '%s' "$2" | tr '\n\t|' '   ' | cut -c1-90 | sed 's/ *$//')"
  tmuxb set-option "@webneed" "$1|$(date +%s)|${_h}"
}
need_clear() { tmuxb set-option -u "@webneed"; }

# The headline for a finished turn: the first line Claude actually said last.
#
# Read from the transcript rather than from the pane, because by the time Stop
# fires the pane may already have scrolled the answer off, and because the
# transcript gives the TEXT - not the box-drawing, the spinner and the token
# counter that a capture would have to be stripped of.
#
# Deliberately quiet: no transcript, no jq, a half-written line - any of them
# just produces an empty headline, and an item with no headline still says which
# session finished. Bounded with tail so a transcript that has grown to tens of
# megabytes over a long session is not read end to end on every Stop.
last_said() {
  _tp="$(printf '%s' "$JSON" | jq -r '.transcript_path // empty' 2>/dev/null)"
  [ -n "$_tp" ] && [ -r "$_tp" ] || return 0
  tail -n 400 "$_tp" 2>/dev/null \
    | jq -r 'select(.type=="assistant")
             | .message.content[]? | select(.type=="text") | .text' 2>/dev/null \
    | grep -v '^[[:space:]]*$' | tail -n 1
}

# Was this session working before it stopped?
#
# The difference between "the build you set off is ready for you" and "you
# pressed enter on an empty prompt". Only the first is news, and the strip is
# a list of things that want you - so a Stop that followed no work clears the
# stamp exactly as it always did.
was_working() {
  case "$(tmuxb show-options -v "@webstate" 2>/dev/null)" in
    working*) return 0 ;;
    *) return 1 ;;
  esac
}

msg="$(printf '%s' "$JSON" | jq -r '.message // empty' 2>/dev/null)"
tool="$(printf '%s' "$JSON" | jq -r '.tool_name // empty' 2>/dev/null)"

# What the pending tool is actually about. Each tool keeps its subject in its
# own field, so the useful headline is per-tool rather than the tool's name -
# "rm -rf build" tells you what to decide; "Bash" does not.
tool_subject() {
  case "$tool" in
    Bash)        printf '%s' "$JSON" | jq -r '.tool_input.command // empty' 2>/dev/null ;;
    Edit|Write|Read|NotebookEdit)
                 printf '%s' "$JSON" | jq -r '(.tool_input.file_path // empty) | split("/") | last' 2>/dev/null ;;
    WebFetch)    printf '%s' "$JSON" | jq -r '.tool_input.url // empty' 2>/dev/null ;;
    Task|Agent)  printf '%s' "$JSON" | jq -r '.tool_input.description // empty' 2>/dev/null ;;
    AskUserQuestion)
                 printf '%s' "$JSON" | jq -r '.tool_input.questions[0].question // empty' 2>/dev/null ;;
    *)           printf '' ;;
  esac
}

case "$EVENT" in
  PermissionRequest)
    stamp attention
    subj="$(tool_subject)"
    need approval "${subj:-${tool:-a permission}}" ;;
  Notification)
    # "Claude is waiting for your input" is just an idle prompt, not a decision
    # to make - leave it green (Stop already stamped idle). Everything else is
    # something that wants you.
    case "$msg" in
      *waiting*input*|*"waiting for your"*) : ;;
      *) stamp attention; need question "${msg:-wants your input}" ;;
    esac ;;
  Stop)
    # Turn finished - whatever was asked has been answered. But a turn that did
    # real work and then stopped is the other thing the strip is for: "the build
    # finished, your move". So the stamp is not cleared, it is rewritten as
    # `done`, carrying the line Claude ended on.
    #
    # The order matters: was_working() reads @webstate, so it has to run BEFORE
    # the stamp is overwritten with idle.
    if was_working; then
      _said="$(last_said)"
      stamp idle
      need done "${_said:-finished}"
    else
      stamp idle
      need_clear
    fi ;;
  SubagentStop)
    stamp idle ;;
  UserPromptSubmit|PostToolUse)
    # Work is moving again, so any prompt got answered.
    stamp working
    need_clear ;;
  PreToolUse)
    # Two tools do not mean "work is moving" at all: they are the turn STOPPING
    # to ask you something, and PreToolUse is the only moment anyone knows which
    # it is. ExitPlanMode and a Bash permission put nearly the same box on the
    # pane, so without this the headline would be whatever text sat above the
    # options.
    case "$tool" in
      ExitPlanMode)
        stamp attention
        need plan "plan ready for your go-ahead" ;;
      AskUserQuestion)
        stamp attention
        subj="$(tool_subject)"
        need question "${subj:-a question for you}" ;;
      *)
        stamp working
        need_clear ;;
    esac ;;
  SessionStart)
    stamp idle
    need_clear ;;
esac
exit 0
