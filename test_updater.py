#!/usr/bin/env python3
"""Real end-to-end test for in-app updates.

No mocks of the parts that matter: real app bundles signed with the real
"Mattdaemon Local Signing" certificate (and ad-hoc, for the ones that must be
refused), real zips, a real HTTP server standing in for GitHub's releases API,
the real codesign checks, and the real swap script swapping a real bundle.
It proves:

  a newer release is offered, an older one or none is not,
  a download that does not match GitHub's checksum is refused,
  a build signed by any other certificate is refused,
  a build that is not the version the release claims is refused,
  a copy with no certificate of its own is told it cannot self-update,
  a good release is installed: old bundle kept as backup, new one in place,
  the swap waits for the app to quit first,
  a failed swap puts the old app back.

Run: python3 test_updater.py   (macOS, needs the signing identity on this Mac)
"""
import hashlib
import importlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SIGN_ID = os.environ.get("MATTDAEMON_SIGN_ID", "Mattdaemon Local Signing")
BUNDLE_ID = "com.mathiassivertsen.mlsuiteterminal"
ASSET = "Mattdaemon-macOS-arm64.zip"

passed = failed = 0


def check(cond, label):
    global passed, failed
    if cond:
        passed += 1
        print("  ✓ " + label)
    else:
        failed += 1
        print("  ✗ " + label)


def make_app(path, version, sign):
    """A minimal real bundle: Info.plist + a Mach-O executable, signed."""
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.join(path, "Contents", "MacOS"))
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": "MLSuiteTerminal",
                       "CFBundleName": "Mattdaemon", "CFBundlePackageType": "APPL",
                       "CFBundleShortVersionString": version, "CFBundleVersion": version}, f)
    exe = os.path.join(path, "Contents", "MacOS", "MLSuiteTerminal")
    shutil.copy("/usr/bin/true", exe)
    subprocess.run(["codesign", "--force", "--sign", sign, path],
                   check=True, capture_output=True)


def app_version(path):
    with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
        return plistlib.load(f)["CFBundleShortVersionString"]


def zip_app(app, out):
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", app, out], check=True)
    with open(out, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Feed:
    """What GitHub's /releases/latest would say. Tests set it per case."""
    release = None          # dict, or None for 404
    files = {}              # url path -> local file


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/latest":
            if Feed.release is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(Feed.release).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        local = Feed.files.get(self.path)
        if not local:
            self.send_response(404)
            self.end_headers()
            return
        with open(local, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def release(base, version, url_path, sha, size):
    return {"tag_name": "v" + version, "draft": False, "prerelease": False,
            "body": "- Test notes for %s" % version, "html_url": base + "/page",
            "published_at": "2026-11-09T00:00:00Z",
            "assets": [{"name": ASSET, "size": size, "digest": "sha256:" + sha,
                        "browser_download_url": base + url_path}]}


def wait_job(up, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if up._job["phase"] not in ("downloading", "verifying"):
            return up._job["phase"]
        time.sleep(0.1)
    return "timeout"


def main():
    if sys.platform != "darwin":
        print("SKIP: macOS only")
        return 0
    ids = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         capture_output=True, text=True).stdout
    if SIGN_ID not in ids:
        print("SKIP: signing identity %r not on this Mac" % SIGN_ID)
        return 0

    work = tempfile.mkdtemp(prefix="mtd-updtest-")
    apps = os.path.join(work, "Applications")
    os.makedirs(apps)
    installed = os.path.join(apps, "Mattdaemon.app")
    upd_dir = os.path.join(work, "updates")
    fixtures = os.path.join(work, "fixtures")
    os.makedirs(fixtures)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    os.environ.update({
        "MATTDAEMON_UPDATE_FEED": base + "/latest",
        "MATTDAEMON_APP_PATH": installed,
        "MATTDAEMON_UPDATES_DIR": upd_dir,
        "MATTDAEMON_UPDATE_NO_QUIT": "1",     # never ask a real app to quit
        "MATTDAEMON_OPEN_CMD": "true",        # and never open one
    })
    import updater as up
    importlib.reload(up)
    cur = up.current_version()
    newer = "%d.%d.%d" % (up.parse_version(cur)[0], up.parse_version(cur)[1] + 1, 0)
    older = "0.0.1"

    def reset():
        up._job.update(phase="idle", progress=0, error=None, version=None)

    # fixtures: the installed copy, a good newer build, and the bad ones
    make_app(installed, cur, SIGN_ID)
    good = os.path.join(fixtures, "good", "Mattdaemon.app")
    make_app(good, newer, SIGN_ID)
    good_zip = os.path.join(fixtures, "good.zip")
    good_sha = zip_app(good, good_zip)
    adhoc = os.path.join(fixtures, "adhoc", "Mattdaemon.app")
    make_app(adhoc, newer, "-")
    adhoc_zip = os.path.join(fixtures, "adhoc.zip")
    adhoc_sha = zip_app(adhoc, adhoc_zip)
    liar = os.path.join(fixtures, "liar", "Mattdaemon.app")
    make_app(liar, "9.9.9", SIGN_ID)
    liar_zip = os.path.join(fixtures, "liar.zip")
    liar_sha = zip_app(liar, liar_zip)
    Feed.files = {"/good.zip": good_zip, "/adhoc.zip": adhoc_zip, "/liar.zip": liar_zip}
    size = lambda p: os.path.getsize(p)

    try:
        print("\nCHECKING (running %s)" % cur)
        Feed.release = None
        st = up.status(force=True)
        check(not st["available"] and not st["error"], "no releases: nothing offered, no error")
        Feed.release = release(base, older, "/good.zip", good_sha, size(good_zip))
        st = up.status(force=True)
        check(not st["available"], "an older release is not offered")
        Feed.release = release(base, newer, "/good.zip", good_sha, size(good_zip))
        st = up.status(force=True)
        check(st["available"] and st["latest"] == newer, "a newer release is offered")
        check(st["canInstall"] is True, "a copy signed with the certificate can install it")
        check(st["release"]["notes"].startswith("- Test notes"), "the notes come along")

        print("\nREFUSING WHAT MUST BE REFUSED")
        reset()
        Feed.release = release(base, newer, "/good.zip", "0" * 64, size(good_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "checksum" in up._job["error"],
              "a download that does not match the checksum is refused")
        check(app_version(installed) == cur, "and the installed app is untouched")

        reset()
        Feed.release = release(base, newer, "/adhoc.zip", adhoc_sha, size(adhoc_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "certificate" in up._job["error"],
              "a build signed by anyone else is refused (%s)" % up._job["error"])
        check(app_version(installed) == cur, "and the installed app is untouched")

        reset()
        Feed.release = release(base, newer, "/liar.zip", liar_sha, size(liar_zip))
        up.status(force=True)
        up.apply_update(app_pid=1, wait=True)
        check(up._job["phase"] == "error" and "says it is" in up._job["error"],
              "a build that is not the version the release claims is refused")

        reset()
        make_app(installed, cur, "-")
        Feed.release = release(base, newer, "/good.zip", good_sha, size(good_zip))
        st = up.status(force=True)
        check(st["canInstall"] is False and "ad-hoc" in (st["cannotInstall"] or ""),
              "an ad-hoc copy is told it cannot update itself")
        r = up.apply_update(app_pid=1)
        check(not r["ok"], "and apply refuses for it")
        make_app(installed, cur, SIGN_ID)

        print("\nINSTALLING A GOOD RELEASE")
        reset()
        fake_app = subprocess.Popen(["sleep", "30"])   # stands in for the running app
        up.status(force=True)
        r = up.apply_update(app_pid=fake_app.pid, wait=True)
        check(r["ok"] and up._job["phase"] == "installing",
              "download + checks pass, the swap is handed over (%s)" % up._job.get("error"))
        time.sleep(2.5)
        check(app_version(installed) == cur, "the swap waits while the app is still running")
        fake_app.terminate()
        fake_app.wait()
        end = time.time() + 20
        while time.time() < end and app_version(installed) != newer:
            time.sleep(0.2)
        check(app_version(installed) == newer, "after it quits, the new version is installed")
        ok = subprocess.run(["codesign", "--verify", "--deep", "--strict", installed]).returncode == 0
        check(ok, "the installed app's signature is intact")
        backup = os.path.join(upd_dir, "Mattdaemon-previous.app")
        check(os.path.isdir(backup) and app_version(backup) == cur, "the old app is kept as a backup")
        state = json.load(open(os.path.join(upd_dir, "state.json")))
        check(state.get("justUpdated", {}).get("version") == newer,
              "the new copy will say what it was updated to")

        print("\nA FAILED SWAP PUTS THE OLD APP BACK")
        make_app(installed, cur, SIGN_ID)
        script = os.path.join(upd_dir, "swap.sh")
        r = subprocess.run(["/bin/bash", script, installed, os.path.join(work, "nope.app"),
                            backup, "999999", ""], capture_output=True, text=True,
                           env=dict(os.environ, MATTDAEMON_OPEN_CMD="true"))
        check(r.returncode != 0, "the swap reports failure")
        check(os.path.isdir(installed) and app_version(installed) == cur,
              "and the old app is back in place")
    finally:
        httpd.shutdown()
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d of %d checks passed." % (passed, passed + failed) if not failed
          else "\n%d of %d checks FAILED." % (failed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
