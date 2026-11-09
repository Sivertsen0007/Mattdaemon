#!/usr/bin/env bash
# Cut a Mattdaemon release: bump VERSION, build, sign, zip, tag, publish.
#
#   ./release.sh patch|minor|major|X.Y.Z [--notes FILE] [--dry-run] [--allow-branch]
#
# Every copy of the app installed from a release checks GitHub for the newest
# one and offers it ("Update now"), so publishing here IS shipping to everyone.
#
# --notes FILE   the release notes people see in the update box. Without it the
#                notes are the commit subjects since the last tag - fine for a
#                quick release, but written notes read far better.
# --dry-run      build and zip into dist/ but change nothing: no commit, no tag,
#                no push, no GitHub release.
# --allow-branch release from a branch other than main.
#
# Must run on the Mac that holds the "Mattdaemon Local Signing" certificate.
# Installed copies only accept an update signed by the same certificate as
# themselves, so a release signed with anything else (or ad-hoc) would be
# refused by every one of them - this script refuses to publish one.
set -euo pipefail
cd "$(dirname "$0")"

BUMP="" NOTES_FILE="" DRY=0 ALLOW_BRANCH=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --notes) NOTES_FILE="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --allow-branch) ALLOW_BRANCH=1; shift ;;
        patch|minor|major) BUMP="$1"; shift ;;
        [0-9]*.[0-9]*.[0-9]*) BUMP="$1"; shift ;;
        *) echo "usage: $0 patch|minor|major|X.Y.Z [--notes FILE] [--dry-run] [--allow-branch]" >&2; exit 2 ;;
    esac
done
[[ -n "$BUMP" ]] || { echo "usage: $0 patch|minor|major|X.Y.Z [--notes FILE] [--dry-run]" >&2; exit 2; }

REPO="Sivertsen0007/Mattdaemon"
ASSET="Mattdaemon-macOS-arm64.zip"
SIGN_ID="${MATTDAEMON_SIGN_ID:-Mattdaemon Local Signing}"

die() { echo "!! $*" >&2; exit 1; }

# ── preflight ──────────────────────────────────────────────────────────────
git diff --quiet && git diff --cached --quiet || die "uncommitted changes - commit or stash them first"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "main" && "$ALLOW_BRANCH" != 1 ]]; then
    die "on branch $BRANCH - releases come from main (or pass --allow-branch)"
fi
security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_ID" \
    || die "signing identity \"$SIGN_ID\" is not on this Mac - releases must be signed with it"

CUR="$(tr -d '[:space:]' < VERSION)"
IFS=. read -r MA MI PA <<<"$CUR"
case "$BUMP" in
    patch) NEW="$MA.$MI.$((PA + 1))" ;;
    minor) NEW="$MA.$((MI + 1)).0" ;;
    major) NEW="$((MA + 1)).0.0" ;;
    *) NEW="$BUMP" ;;
esac
python3 - "$CUR" "$NEW" <<'EOF' || die "new version $NEW is not newer than $CUR"
import sys
a, b = (tuple(map(int, v.split("."))) for v in sys.argv[1:3])
sys.exit(0 if b > a else 1)
EOF
git rev-parse -q --verify "refs/tags/v$NEW" >/dev/null && die "tag v$NEW already exists"

PREV_TAG="$(git describe --tags --abbrev=0 2>/dev/null || true)"
NOTES="$(mktemp)"
trap 'rm -f "$NOTES"' EXIT
if [[ -n "$NOTES_FILE" ]]; then
    cp "$NOTES_FILE" "$NOTES"
else
    git log ${PREV_TAG:+"$PREV_TAG"..HEAD} --no-merges --format='- %s' > "$NOTES"
fi
[[ -s "$NOTES" ]] || die "release notes are empty"

echo "==> Mattdaemon $CUR -> $NEW (from $BRANCH, previous tag ${PREV_TAG:-none})"
echo "---- notes ----"; cat "$NOTES"; echo "---------------"

# ── build ──────────────────────────────────────────────────────────────────
echo "$NEW" > VERSION
restore_version() { [[ "$DRY" == 1 ]] && echo "$CUR" > VERSION; true; }
trap 'restore_version; rm -f "$NOTES"' EXIT

./macapp/build.sh release
APP="macapp/build/Mattdaemon.app"
# -dvv: the Authority line only shows from the second level of detail. Read
# into a variable first so grep -q cannot cut codesign off under pipefail.
SIGINFO="$(codesign -dvv "$APP" 2>&1 || true)"
SIGREQ="$(codesign -d -r- "$APP" 2>&1 || true)"
[[ "$SIGINFO" == *"Authority=$SIGN_ID"* && "$SIGREQ" == *"certificate leaf"* ]] \
    || die "the build is not signed by \"$SIGN_ID\" - refusing to publish it"
[[ "$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Contents/Info.plist")" == "$NEW" ]] \
    || die "the build does not carry version $NEW"

mkdir -p dist
rm -f "dist/$ASSET"
ditto -c -k --keepParent "$APP" "dist/$ASSET"
SHA="$(shasum -a 256 "dist/$ASSET" | cut -d' ' -f1)"
echo "==> dist/$ASSET  sha256 $SHA"

if [[ "$DRY" == 1 ]]; then
    echo "==> Dry run: nothing committed, tagged or published. VERSION is back at $CUR."
    exit 0
fi

# ── publish ────────────────────────────────────────────────────────────────
git add VERSION
git commit -q -m "Release v$NEW"
git tag -a "v$NEW" -F "$NOTES"
git push origin "$BRANCH"
git push origin "v$NEW"

if [[ -z "${GH_TOKEN:-}" ]]; then
    # git already talks to GitHub through the keychain; gh can borrow that token.
    GH_TOKEN="$(printf 'host=github.com\nprotocol=https\n\n' \
        | git -c credential.helper=osxkeychain credential fill 2>/dev/null \
        | sed -n 's/^password=//p')"
    export GH_TOKEN
fi
gh release create "v$NEW" "dist/$ASSET" -R "$REPO" \
    --title "Mattdaemon $NEW" --notes-file "$NOTES"
echo "==> Published v$NEW. Installed copies offer it at their next start, or within a day."
