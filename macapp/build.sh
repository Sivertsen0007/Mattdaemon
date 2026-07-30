#!/usr/bin/env bash
# Build MLSuiteTerminal.app from SPM output and bundle the Python engine so the
# .app is fully self-contained (double-click, no external files needed).
set -euo pipefail

cd "$(dirname "$0")"

CONFIG="${1:-release}"
echo "==> swift build -c $CONFIG"
swift build -c "$CONFIG"

BIN_PATH="$(swift build -c "$CONFIG" --show-bin-path)"
APP="build/MLSuiteTerminal.app"

echo "==> Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1. Native binary + Info.plist + app icon
cp "$BIN_PATH/MLSuiteTerminal" "$APP/Contents/MacOS/MLSuiteTerminal"
cp Resources/Info.plist "$APP/Contents/Info.plist"
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# 2. Python engine (lives one level up in mac-terminal/). Copied into the
#    bundle's Resources so the app carries its own server.
echo "==> Bundling Python engine into Contents/Resources"
cp ../server.py "$APP/Contents/Resources/server.py"
cp ../terminal_manager.py "$APP/Contents/Resources/terminal_manager.py"
rm -rf "$APP/Contents/Resources/static"
cp -R ../static "$APP/Contents/Resources/static"

# 3. Ad-hoc codesign so Gatekeeper lets a locally-built app launch.
echo "==> Ad-hoc codesigning"
codesign --force --deep --sign - "$APP"

echo "==> Done: $APP"
echo "Run with: open $APP   (or: $APP/Contents/MacOS/MLSuiteTerminal for console logs)"
