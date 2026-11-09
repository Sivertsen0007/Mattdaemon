#!/usr/bin/env bash
# Build Mattdaemon.app from SPM output and bundle the Python engine so the
# .app is fully self-contained (double-click, no external files needed).
# (The executable inside stays MLSuiteTerminal - the SPM product name.)
set -euo pipefail

cd "$(dirname "$0")"

# Usage: ./build.sh [release|debug] [--install]
#   --install  also puts the app in ~/Applications (or $MATTDAEMON_INSTALL_DIR),
#              quitting a running copy first and starting it again after.
CONFIG="release"
INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        release|debug) CONFIG="$arg" ;;
        *) echo "usage: $0 [release|debug] [--install]" >&2; exit 2 ;;
    esac
done
echo "==> swift build -c $CONFIG"
swift build -c "$CONFIG"

BIN_PATH="$(swift build -c "$CONFIG" --show-bin-path)"
OUT="build/Mattdaemon.app"

# The bundle is assembled and signed in a temporary folder OUTSIDE the repo and
# only then copied to build/. A repo under ~/Desktop or ~/Documents is usually
# synced by iCloud, whose File Provider keeps putting its own attributes back on
# every file there - faster than any xattr -c can take them off - and codesign
# refuses such a bundle with "resource fork, Finder information, or similar
# detritus not allowed", leaving it unsigned. In a temp folder nothing touches
# it, and a signature made there stays valid when the finished app is copied in.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mattdaemon-build.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/Mattdaemon.app"

echo "==> Assembling $OUT (staged in $STAGE)"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1. Native binary + Info.plist + app icon
cp "$BIN_PATH/MLSuiteTerminal" "$APP/Contents/MacOS/MLSuiteTerminal"
cp Resources/Info.plist "$APP/Contents/Info.plist"
# The version lives in one place, ../VERSION, which release.sh bumps. Stamped
# into the bundle here so Finder, About and the updater all agree on it.
VERSION="$(tr -d '[:space:]' < ../VERSION)"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist"
echo "==> Version $VERSION"
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# 2. Python engine (lives one level up in mac-terminal/). Copied into the
#    bundle's Resources so the app carries its own server.
echo "==> Bundling Python engine into Contents/Resources"
cp ../server.py "$APP/Contents/Resources/server.py"
cp ../terminal_manager.py "$APP/Contents/Resources/terminal_manager.py"
cp ../terminal_worktree.py "$APP/Contents/Resources/terminal_worktree.py"
cp ../setup.py "$APP/Contents/Resources/setup.py"
cp ../lifeboat.py "$APP/Contents/Resources/lifeboat.py"
cp ../updater.py "$APP/Contents/Resources/updater.py"
cp ../bugreport.py "$APP/Contents/Resources/bugreport.py"
cp ../VERSION "$APP/Contents/Resources/VERSION"
rm -rf "$APP/Contents/Resources/static"
cp -R ../static "$APP/Contents/Resources/static"

# 2a. The bundled engine must be able to import itself. A module added to the
#     project but forgotten in the copy list above is otherwise a crash on first
#     launch, and every step between here and there would have looked fine.
echo "==> Verifying the bundled engine imports"
if ! ( cd "$APP/Contents/Resources" && python3 -c "import terminal_worktree, terminal_manager, setup, updater, bugreport" ); then
    echo "!! the bundled engine does not import - a module is missing from the copy list above" >&2
    exit 1
fi

# 2b. Nothing personal travels with the app. mts-config.json holds the working
#     folder and, when one is connected, a VPS token - per-user state that lives
#     in Application Support and must never end up in a bundle somebody else
#     will hold. Fail the build rather than ship one.
if [[ -e "$APP/Contents/Resources/mts-config.json" ]]; then
    echo "!! mts-config.json ended up inside the app - refusing to build" >&2
    exit 1
fi

# 3. Codesign, so Gatekeeper lets a locally-built app launch.
#
#    With a signing identity if this Mac has one, and that is not cosmetic: an
#    ad-hoc signature gives macOS nothing to identify the app by except the hash
#    of the binary, so every rebuild reads as a brand new app and every privacy
#    permission you granted - Documents, Desktop, Full Disk Access - is thrown
#    away. Signed with a stable identity, the permissions you grant once keep
#    working for every build after it.
#
#    Run ./make-signing-cert.sh once to create one. Without it this falls back
#    to ad-hoc, which still builds and still runs - it just forgets.
#
#    Quarantine and other attributes copied in with the sources are stripped
#    first; codesign rejects a bundle carrying Finder info or a resource fork.
xattr -cr "$APP" 2>/dev/null || true

SIGN_ID="${MATTDAEMON_SIGN_ID:-Mattdaemon Local Signing}"
if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_ID"; then
    echo "==> Codesigning as \"$SIGN_ID\""
    codesign --force --deep --sign "$SIGN_ID" "$APP"
else
    echo "==> Ad-hoc codesigning (no signing identity found)"
    echo "    Privacy permissions will be forgotten on every rebuild."
    echo "    Run ./make-signing-cert.sh once to stop that."
    codesign --force --deep --sign - "$APP"
fi

codesign --verify --deep --strict "$APP"

# 4. Hand the signed app over to build/. ditto keeps the signature intact.
rm -rf "$OUT"
mkdir -p "$(dirname "$OUT")"
ditto "$APP" "$OUT"
codesign --verify --deep "$OUT"

echo "==> Done: $OUT"

if [[ "$INSTALL" != 1 ]]; then
    echo "Run with: open $OUT   (or: $OUT/Contents/MacOS/MLSuiteTerminal for console logs)"
    echo "Or rebuild with --install to put it in ~/Applications."
    exit 0
fi

# 5. Install. Copied from the clean staged bundle, never from build/: a repo in
#    an iCloud folder hands its attributes on to anything copied out of it, and
#    the installed app would then fail a strict signature check. --noextattr
#    keeps them off; Finder can still tag the bundle folder itself with
#    FinderInfo the moment it appears, so that is stripped too before checking.
DEST_DIR="${MATTDAEMON_INSTALL_DIR:-$HOME/Applications}"
DEST="$DEST_DIR/Mattdaemon.app"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP/Contents/Info.plist")"

# Sessions live in tmux, so quitting the app loses nothing - they are all
# there again when it starts. Replacing a bundle that is running is what breaks.
WAS_RUNNING=0
if pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1; then
    WAS_RUNNING=1
    echo "==> Quitting the running Mattdaemon"
    osascript -e "quit app id \"$BUNDLE_ID\"" >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1 || break
        sleep 0.5
    done
    if pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1; then
        echo "!! Mattdaemon did not quit - close it and run again" >&2
        exit 1
    fi
fi

echo "==> Installing to $DEST"
mkdir -p "$DEST_DIR"
rm -rf "$DEST"
ditto --noextattr --noqtn "$APP" "$DEST"
xattr -rd com.apple.FinderInfo "$DEST" 2>/dev/null || true
codesign --verify --deep --strict "$DEST"

if [[ "$WAS_RUNNING" == 1 ]]; then
    echo "==> Starting it again"
    open "$DEST"
fi
echo "==> Installed: $DEST"
