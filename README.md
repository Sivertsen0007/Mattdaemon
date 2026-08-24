# Mattdaemon - Claude Code in a terminal that keeps running

A small, self-contained Mac app that runs tmux-backed terminal sessions with
Claude Code in them. Sessions survive closing the window, so you can come back
to whatever Claude was doing. It is the same terminal as the AI-Hub dashboard at
`:3333`, pointed at your own machine and served on loopback only (`127.0.0.1`) -
nothing is exposed to the network.

**It carries nobody's account.** On first launch it asks for a Claude Code login
(your own subscription) and the folder your sessions should open in, and stores
both per user outside the app bundle - so the `.app` can be handed to someone
else as-is. See [First run](#first-run) and [Give it to someone else](#give-it-to-someone-else).

Two ways to run it:

- **Native app** - a real `.app` you double-click. Needs a one-off build on the
  Mac (Swift toolchain / Xcode command-line tools).
- **No build** - `./run-terminal.sh` starts the Python engine and opens it in
  your browser. No Xcode, no compilation. Good enough for daily use.

Under the hood both paths run the same four pieces: `server.py` (a tiny loopback
HTTP server), `terminal_manager.py` (the tmux session logic), `setup.py`
(first-run state and the per-user config), and the `static/` frontend.

## First run

The first launch shows a setup card instead of the terminal. Three steps:

1. **Requirements** - it looks for Claude Code and tmux on this Mac and shows the
   install command for whichever is missing. Neither is bundled; both are yours.
2. **Your folder** - where new sessions open, so Claude starts inside your
   project rather than your home directory. `Choose...` opens a normal macOS
   folder picker in the `.app`, or an in-page folder list in the browser.
3. **Sign in to Claude** - runs `claude auth login --claudeai` in a real terminal
   inside the card and opens your browser. You sign in with **your own** Claude
   subscription. The app never sees a password or a token: it types the command
   and waits for Claude Code to report itself signed in.

Nothing can open a shell until that is done - the server refuses, not just the
page. The session you signed in through is kept and renamed after your folder,
so you land straight in a working session.

Afterwards all three live behind the gear in the sidebar, along with the
optional VPS. Signing in as someone else, changing folder, or re-running the
whole setup are all there.

## Where your settings live

    ~/Library/Application Support/MLSuiteTerminal/mts-config.json

Per user, outside the app bundle, `0600` because it can hold a VPS token. The
app writes it; you never have to. A `mts-config.json` next to `server.py` is
still read as a fallback for a source checkout, and never written.

## Build the native app (on the Mac)

```sh
cd macapp && ./build.sh
open build/Mattdaemon.app
```

`build.sh` runs `swift build`, assembles `Mattdaemon.app`, bundles the
Python engine and `static/` into it, and ad-hoc codesigns it so Gatekeeper lets
it launch. The result is fully self-contained - you can move the `.app`
anywhere and double-click it.

Note: `swift build` only runs on macOS, so the native app can only be built on a
Mac. On any other platform, use the no-build path below.

## Run without building

```sh
./run-terminal.sh
```

This starts `python3 server.py --port 8722` in the background, waits until it
answers on `http://127.0.0.1:8722/`, then opens that URL in your default
browser - setup card included, if you have not been through it. Press `Ctrl-C`
in the terminal to stop the server; the script traps the exit and shuts down the
server it started.

Requirements: `python3`, `tmux`, and `curl` on your `PATH` (all standard on a
Mac, `tmux` via Homebrew if you have not got it: `brew install tmux`).

## How sessions persist

Terminal sessions are backed by **tmux**, so they survive closing the app or the
browser tab - reopen it and your shells, running processes, and scrollback are
still there. They do **not** survive a full reboot: tmux is torn down when the
machine restarts, so you start fresh after a reboot.

## VPS sessions in the same window (optional, off by default)

Connect an AI-Hub dashboard under **Settings -> VPS** (address + token) and its
`:3333` terminal sessions appear in a **VPS** section of the sidebar alongside
the local ones, and can be driven from here. Nothing is saved until the box
answers with that token. The token is held by this server and proxied - it never
reaches the browser, and it never travels inside the app bundle. Each section
gets its own `+`, so a session can be started on either machine.

Until someone connects one, there is no VPS section and no mention of it outside
Settings: a fresh install is a purely local terminal.

## Copy and paste

**Marking text copies it.** Drag across anything in a terminal and it is on the
clipboard when you let go - no second action, ⌘V works straight away. ⌘C does
the same for whatever is selected, and is left alone when nothing is, so it
stays the interrupt the shell expects.

Worth saying why this needed writing at all: xterm keeps its own selection
model, so the highlighted text is not a DOM selection. WebKit's own Copy - the
Edit menu item, and therefore ⌘C - had nothing to copy, and quietly left the
clipboard holding whatever was in it before. The app owns the copy now: in the
`.app` it goes over the bridge to `NSPasteboard`, which has no user-gesture and
no secure-context rule to trip over; in a browser it uses `navigator.clipboard`,
falling back to a hidden textarea with focus saved and restored. It says
"Copied" only when one of those actually succeeded.

Pasting in is unchanged: ⌘V types into the shell, and a pasted screenshot is
uploaded into the session instead.

## Send a session to the VPS

Right-click a local session -> **Send to VPS**. A running process cannot be
moved between machines, so this is a *handoff*, not a migration:

1. The local Claude is asked to write a handoff brief - what is being built,
   what is done, what is next, key files, gotchas.
2. A new session starts on the box, `cd`s to the mapped folder, and opens
   Claude on that brief.
3. **The local session keeps running.** Nothing closes it.

The brief travels through a quoted heredoc, so prose containing `$`, backticks
or quotes arrives byte-identical. Folders are translated via `vps.path_map`; a
session in an unmapped folder is refused rather than dropped somewhere
unrelated. If the working tree is dirty you are shown what is uncommitted and
asked to confirm first - the box has its own clone and will not see local
changes until they are pushed. A session running a plain shell has no
conversation to hand over, so it is simply reopened in the mapped folder.

The mapping is per user, set in **Settings -> VPS -> Folder mapping**, one
`local = remote` per line. There is no built-in default: one machine's folder
layout is nobody else's.

## Bring a session back from the VPS

Right-click a **VPS** session -> **Bring to Mac**. The mirror of the above, with
the part the outbound direction does not need: the files.

Going out, the box already has its own clone and only needs to know what to do.
Coming back, this Mac is behind by whatever the box has been building, so a
brief on its own would describe files that are not here. So:

1. The box's Claude is asked to **commit and push** its work first, then write
   the brief - which starts with a fixed `CWD / BRANCH / COMMIT / PUSHED`
   header, so this end knows exactly which commit to wait for.
2. This Mac fetches until that commit is actually here, and **fast-forwards**
   the local clone to it.
3. A local session opens in the matching folder, named `<the VPS name> (VPS
   handover)`, with Claude started on the brief - and the brief it gets is
   prefixed with where the work came from and exactly what git did.
4. **The VPS session keeps running.** Nothing closes it.

Git is deliberately two separate steps. Fetching is always safe and is what
brings the files across, so it happens whatever state the tree is in. Merging is
not: an uncommitted local tree, or a clone sitting on a different branch, means
you have something of your own here, and moving your HEAD under you would be
help nobody asked for. That case fetches, says so in plain words, and leaves the
decision to the session that is about to open. A dirty tree is flagged *before*
the box is asked for anything, so a minute of its Claude is not spent on a
handover you then cancel.

The brief is read back off the box through its own file endpoint, so no shell
is needed on the far side. It is written into the repo's `.git` directory:
inside the folder that endpoint serves, and somewhere git itself never looks, so
a handover never leaves a stray file in the working tree. The file does stay
there afterwards - the box's pane is busy running Claude, so there is nothing to
type an `rm` into. `find . -name 'mts-handoff-*.md' -path '*/.git/*'` clears them
if they ever bother you.

The folder mapping is the same one, read backwards, so a session sent out and
brought back lands in the folder it started in.

## Change the working directory

**Settings -> Working folder** (the gear at the bottom of the sidebar). It
applies to sessions you open from then on; the ones already running stay where
they are, because a running shell cannot be moved.

## Give it to someone else

The `.app` is self-contained and personal to nobody: no folder, no login, no
token travels with it. Copy `build/Mattdaemon.app` across and it opens on the
setup card for whoever launches it, against their own Claude subscription.

- Everything personal lives in `~/Library/Application Support/MLSuiteTerminal/`,
  which is per user and never inside the bundle. `build.sh` fails the build if a
  `mts-config.json` ever ends up in `Contents/Resources`.
- The Claude login is Claude Code's own, in their Keychain. This app only asks it
  who is signed in, and it never handles a credential itself.
- They need Claude Code and tmux installed; step 1 of setup tells them so, with
  the command to fix it.
- Building on their machine instead? `git clone`, then `cd macapp && ./build.sh`.
- To hand over your own copy of the app, or to test the first-run experience:
  **Settings -> Run first-time setup again**.

Gatekeeper note: the app is ad-hoc signed, so a copy from another Mac needs
right-click -> **Open** the first time (or `xattr -dr com.apple.quarantine
Mattdaemon.app`).

## Optional: run at login via launchd (future add)

Not built yet, described here so it is on record. To have the terminal server
start automatically at login, you would add a per-user launchd agent - a
`~/Library/LaunchAgents/com.aihub.mtsterminal.plist` that runs `run-terminal.sh`
(or `server.py` directly) with `RunAtLoad` set, loaded via `launchctl load`.
That would keep the server always up in the background so the app opens
instantly. Left as a future addition rather than shipped, to avoid installing a
background agent people did not ask for.

## Verified

Integration smoke passes on Linux against the fully merged tree. The engine
E2E - `python3 server.py --selftest` - boots the real loopback HTTP server,
starts a tmux-backed session, streams its output, types `echo` of a token, and
confirms that token round-trips back out of a real shell (prints `VERIFY_OK`,
exit 0). The page-serving smoke confirms `server.py` serves the real UI:
`GET /` returns `static/index.html` (200, `text/html`) and every asset the page
references - the vendored `xterm.css`, `xterm.js`, and `addon-fit.js` under
`/static/vendor/` - returns 200 with the correct content type, so the terminal
front end loads with no missing dependencies.

Setup has its own two, plus a browser pass:

- `python3 server.py --selftest-setup` - drives the setup API against a
  throwaway config (`MTS_CONFIG`), no tmux and no network needed. 17 checks: a
  fresh install needs setup, opening a shell is refused until it is finished, a
  folder that does not exist is rejected, a real one is stored and takes effect
  without a restart, the folder browser lists sub-folders, a VPS that does not
  answer is not saved, an empty address disconnects, `complete` refuses without
  a login, `reset` puts it back to first-run, and the config is written `0600`.
- A stub-CLI test proves the sign-in step really drives a shell: with a fake
  `claude` first on `PATH`, `/api/setup/login` opens a session and the pane comes
  back showing `claude auth login --claudeai` running from the resolved absolute
  path - so the wiring is verified without touching a real Claude login.
- A Playwright pass walks the card as a new user in a real browser: both
  requirements detected, the home directory (not anyone's project) pre-filled, a
  bad folder refused with a reason, the existing login recognised, the summary
  correct, and the app on screen with `setupComplete` written - no JS errors.

Copy is verified where it was broken - against a real terminal, reading the real
clipboard. A Playwright pass opens a session, echoes a token, drags the mouse
across it, and asserts the *system clipboard* now holds exactly that text (the
clipboard is loaded with something else first, so "it was already there" cannot
pass for a copy); then clears it and proves ⌘C does the same. The toast is
checked too, because "it said Copied and copied nothing" was the original bug.
The `.app`'s leg of it - the bridge to `NSPasteboard` - rides the same
`messageHandlers.mts` dispatch that the folder picker and pop-out windows
already use, and the pasteboard write itself is asserted directly.

The handover back from the box - `python3 server.py --selftest-handover`, no
tmux and no network - covers the parts that could be wrong quietly. 20 checks:
the folder map read backwards (longest prefix wins, an unmapped folder is
refused, and out-and-back lands where it started), the brief's four header
lines, a brief with no header at all, where the box is told to write it, and
then the git step against **real throwaway repositories** - a clean clone
fast-forwards and the box's file really appears on the Mac; a dirty tree gets
the commit fetched but is *not* merged and the user's uncommitted file is
untouched; a commit that was never pushed is reported rather than waited on for
ever; a missing local folder is reported.

`swift build -c release` compiles the app with the folder-picker bridge; run
`./macapp/build.sh` on a Mac to produce the `.app` (Swift builds only on macOS).
The no-build path works everywhere.
