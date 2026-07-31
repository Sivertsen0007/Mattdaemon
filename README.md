# Mattdaemon - the VPS web terminal as a local Mac app

This is a small, self-contained wrapper that runs the AI-Hub web terminal
locally on a Mac. It is the same tmux-backed terminal you use in the VPS
dashboard at `:3333`, but pointed at your own machine and served on loopback
only (`127.0.0.1`) - so there is no auth and nothing is exposed to the network.

Two ways to run it:

- **Native app** - a real `.app` you double-click. Needs a one-off build on the
  Mac (Swift toolchain / Xcode command-line tools).
- **No build** - `./run-terminal.sh` starts the Python engine and opens it in
  your browser. No Xcode, no compilation. Good enough for daily use.

Under the hood both paths run the same three pieces: `server.py` (a tiny
loopback HTTP server), `terminal_manager.py` (the tmux session logic), and the
`static/` frontend.

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

This reads your working directory from `mts-config.json`, starts
`python3 server.py --port 8722` in the background, waits until it answers on
`http://127.0.0.1:8722/`, then opens that URL in your default browser. Press
`Ctrl-C` in the terminal to stop the server - the script traps the exit and
shuts down the server it started.

Requirements: `python3`, `tmux`, and `curl` on your `PATH` (all standard on a
Mac, `tmux` via Homebrew if you have not got it: `brew install tmux`).

## How sessions persist

Terminal sessions are backed by **tmux**, so they survive closing the app or the
browser tab - reopen it and your shells, running processes, and scrollback are
still there. They do **not** survive a full reboot: tmux is torn down when the
machine restarts, so you start fresh after a reboot.

## VPS sessions in the same window

With a `vps` block in `mts-config.json` (see `mts-config.json.example`), the
box's `:3333` terminal sessions appear in a **VPS** section of the sidebar
alongside the local ones, and can be driven from here. The token is held by
this server and proxied - it never reaches the browser. Each section has its
own `+`, so a session can be started on either machine. Without the block, the
app behaves exactly as local-only.

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

## Change the working directory

New terminal sessions start in the directory set by the `home` key in
`mts-config.json` (in this folder). The default is `~/Documents/AI-Hub`.

The file is created automatically the first time you run `./run-terminal.sh`.
See `mts-config.json.example` for the shape:

```json
{
  "home": "~/Documents/AI-Hub",
  "port": 8722
}
```

Edit `home` to point anywhere you like (a `~` is expanded), then restart. The
`.app` and the script both read the same file.

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
front end loads with no missing dependencies. The only remaining step is to run
`./macapp/build.sh` on a Mac to produce the native `.app` (Swift builds only on
macOS); the no-build path already works everywhere.
