#!/usr/bin/env bash
# Pull every bug report from the bug desk on the VPS and list the new ones.
#
#   bugdesk/pull.sh [--all]
#
# Reports are copied (never moved) over SSH into ~/Documents/Mattdaemon-bugs,
# one folder per month, and the ones not seen at the last pull are summarised.
# --all lists every report, not only the new ones. Needs the vps-root SSH host.
set -euo pipefail
HOST="${MATTDAEMON_BUGS_HOST:-vps-root}"
REMOTE="${MATTDAEMON_BUGS_REMOTE:-/home/aihub/mattdaemon-bugs/}"
LOCAL="${MATTDAEMON_BUGS_LOCAL:-$HOME/Documents/Mattdaemon-bugs}"
ALL=0; [[ "${1:-}" == "--all" ]] && ALL=1

mkdir -p "$LOCAL"
rsync -a --exclude '.*.tmp' "$HOST:$REMOTE" "$LOCAL/"

python3 - "$LOCAL" "$ALL" <<'PY'
import glob, json, os, sys
local, show_all = sys.argv[1], sys.argv[2] == "1"
seen_file = os.path.join(local, ".seen")
seen = set(open(seen_file).read().split()) if os.path.exists(seen_file) else set()
reports = []
for p in sorted(glob.glob(os.path.join(local, "*", "*.json"))):
    try:
        r = json.load(open(p))
    except ValueError:
        continue
    r["_path"] = p
    reports.append(r)
new = [r for r in reports if r["id"] not in seen]
shown = reports if show_all else new
print("%d reports in total, %d new since the last pull.\n" % (len(reports), len(new)))
for r in shown:
    m = r.get("meta") or {}
    print("-" * 72)
    print("%s  v%s  macOS %s  %s%s" % (r["received"], r.get("version") or "?", m.get("macos") or "?",
                                     m.get("page") or "?", "  [screenshot]" if r.get("screenshot") else ""))
    if r.get("contact"):
        print("From: " + r["contact"])
    print(r["text"])
    if r.get("expected"):
        print("Expected: " + r["expected"])
    print(r["_path"])
with open(seen_file, "w") as f:
    f.write("\n".join(sorted(seen | {r["id"] for r in reports})))
PY
