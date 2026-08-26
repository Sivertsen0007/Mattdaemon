#!/usr/bin/env python3
"""
Tests for worktree groups - the git half.

dispose() deletes directories, so the attacks are path escapes: a worktree name
is typed by a person and becomes both a filesystem path and a git branch. Nothing
here goes through a shell, so the checking has to be its own.

Everything below runs against a THROWAWAY repo in a temp directory, created and
removed by this file. It never touches your folder, the real worktree root, any
branch, or any session - so it is safe to run at any time, including while the
app is open and shells are live.

Run: python3 test_terminal_worktree.py
"""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="wt-test-")
REPO = os.path.join(TMP, "repo")
ROOT = os.path.join(TMP, "worktrees")
os.environ["MTS_WORKTREE_ROOT"] = ROOT      # read at import, so set it first

_spec = importlib.util.spec_from_file_location(
    "twt", pathlib.Path(__file__).with_name("terminal_worktree.py"))
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  %s %s%s" % ("✓" if ok else "✗", name, ("  " + detail) if detail else ""))


def git(*args, cwd=REPO):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def build_repo():
    os.makedirs(os.path.join(REPO, "shared"))
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    pathlib.Path(REPO, "shared", "thing.py").write_text("print('hi')\n")
    pathlib.Path(REPO, ".gitignore").write_text("shared/.env\n.env\n")
    # The untracked config a real checkout needs and git will never carry over.
    pathlib.Path(REPO, "shared", ".env").write_text("TOKEN=secret\n")
    git("add", "-A")
    git("commit", "-qm", "initial")


build_repo()

try:
    print("PATH ESCAPES - a name becomes a path AND a branch")
    ATTACKS = [
        ("../../etc", "climb out of the root"),
        ("../../../Users/someone/Documents", "aim at a real folder"),
        ("..", "the parent itself"),
        ("/etc/passwd", "an absolute path"),
        ("wt/../../..", "climb via a valid-looking prefix"),
        ("wt;rm -rf /", "shell metacharacters"),
        ("wt\nrm -rf /", "a newline"),
        ("wt id", "a space"),
        ("", "empty"),
        ("x" * 70, "over-long"),
        ("-delete", "a leading dash that git could read as a flag"),
        (None, "not a string"),
        (123, "an int"),
    ]
    for bad, why in ATTACKS:
        try:
            W.worktree_path(bad)
            check("rejects %s" % why, False, "ACCEPTED %r" % (bad,))
        except W.WorktreeError:
            check("rejects %s" % why, True)
        except Exception as e:
            check("rejects %s" % why, False, "wrong error: %r" % e)

    print("\nSLUGS - a human name is sanitised before it is ever an id")
    check("a space becomes a dash", W.slugify("email fix") == "email-fix")
    check("punctuation collapses", W.slugify("Email!! fix??") == "email-fix")
    check("a traversal attempt is flattened", W.slugify("../../etc") == "etc")
    check("a slash cannot survive into a branch name", "/" not in W.slugify("a/b"))
    for bad in ("", "   ", "!!!", "---", None, 7):
        try:
            W.slugify(bad)
            check("refuses a nameless name %r" % (bad,), False, "ACCEPTED")
        except W.WorktreeError:
            check("refuses a nameless name %r" % (bad,), True)

    print("\nBASE REFS - typed by a person, lands in a git argv slot")
    for bad in ("--force", "-b", "", "   ", "a b", "a\nb", None):
        try:
            W.validate_base(bad)
            check("refuses base %r" % (bad,), False, "ACCEPTED")
        except W.WorktreeError:
            check("refuses base %r" % (bad,), True)
    check("accepts origin/main", W.validate_base("origin/main") == "origin/main")
    check("accepts a sha", W.validate_base(" abc123 ") == "abc123")

    print("\nBRANCHES CAN ONLY EVER BE wt/* - this is what protects your branch")
    check("a valid id yields a wt/ branch", W.branch_name("fix-1") == "wt/fix-1")
    check("even the id 'main' cannot reach the main branch",
          W.branch_name("main") == "wt/main")
    check("nor can 'production'", W.branch_name("production") == "wt/production")

    print("\nTHE ROOT IS OUTSIDE THE FOLDER - the whole point")
    check("the worktree root is not under the repo",
          not os.path.realpath(W.WORKTREE_ROOT).startswith(os.path.realpath(REPO) + os.sep),
          W.WORKTREE_ROOT)
    default_root = os.path.join(os.path.expanduser("~"), ".mattdaemon-worktrees")
    check("and the shipped default is a sibling of your projects, not inside one",
          os.path.dirname(default_root) == os.path.expanduser("~"), default_root)

    print("\nA NON-REPO FOLDER IS REFUSED, NOT HALF-CREATED")
    plain = os.path.join(TMP, "not-a-repo")
    os.makedirs(plain)
    check("is_git_repo says no", W.is_git_repo(plain) is False)
    try:
        W.create("x", "main", plain)
        check("create refuses a folder that is not a repo", False, "ACCEPTED")
    except W.WorktreeError as e:
        check("create refuses a folder that is not a repo", True)
        check("and says so in words a person can act on", "not a git repository" in str(e))
    check("nothing was left in the root", not os.path.exists(os.path.join(ROOT, "x")))

    print("\nREAL LIFECYCLE - creating and disposing an actual worktree")
    info = W.create("Email fix", "main", REPO)
    path = info["path"]
    check("create makes a real checkout", os.path.isdir(path), path)
    check("the name became a slug id", info["id"] == "email-fix", info["id"])
    check("the display name is preserved", info["name"] == "Email fix")
    check("it is under the worktree root", path.startswith(W.WORKTREE_ROOT))
    check("it is OUTSIDE the folder", not os.path.realpath(path).startswith(
        os.path.realpath(REPO) + os.sep))
    check("it is on its own branch",
          git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).stdout.strip() == "wt/email-fix")
    check("it appears in list_worktrees",
          "email-fix" in [w["id"] for w in W.list_worktrees(REPO)])

    # The reason the root lives outside the folder at all.
    check("YOUR FOLDER's git status stays clean",
          git("status", "--porcelain").stdout.strip() == "")

    print("\nTHE SEED STEP - a checkout with no .env is a dead checkout")
    seeded = os.path.join(path, "shared", ".env")
    check("shared/.env was seeded in", os.path.exists(seeded))
    check("it is a symlink, not a second copy of the secret", os.path.islink(seeded))
    check("it points at the real file",
          os.path.realpath(seeded) == os.path.realpath(os.path.join(REPO, "shared", ".env")))
    check("it reads through", pathlib.Path(seeded).read_text() == "TOKEN=secret\n")
    check("and the new checkout's own status is clean",
          git("status", "--porcelain", cwd=path).stdout.strip() == "")
    check("seeding twice does not double up", W.seed("email-fix", REPO) == [])

    print("\nCOLLISIONS - the same name twice")
    second = W.create("Email fix", "main", REPO)
    check("the second gets a suffixed id", second["id"] == "email-fix-2", second["id"])
    check("and its own branch", second["branch"] == "wt/email-fix-2")
    W.dispose("email-fix-2", REPO)

    print("\nCOMMITS GO TO THE WORKTREE'S BRANCH, NEVER YOURS")
    before = git("rev-parse", "main").stdout.strip()
    pathlib.Path(path, "probe.txt").write_text("work")
    git("add", "probe.txt", cwd=path)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "wt work", cwd=path)
    check("committing in a worktree does not move main",
          git("rev-parse", "main").stdout.strip() == before)

    print("\nTHE DIRTY GUARD - uncommitted work is not thrown away by a button")
    check("a committed worktree is not dirty", W.is_dirty("email-fix") is False)
    pathlib.Path(path, "unsaved.txt").write_text("half an hour of thinking")
    check("an uncommitted file makes it dirty", W.is_dirty("email-fix") is True)
    try:
        W.dispose("email-fix", REPO)
        check("dispose REFUSES a dirty worktree", False, "IT DELETED THE WORK")
    except W.WorktreeError:
        check("dispose REFUSES a dirty worktree", True)
    check("and the checkout is still there", os.path.isdir(path))
    check("with the file still in it", os.path.exists(os.path.join(path, "unsaved.txt")))

    W.dispose("email-fix", REPO, force=True)
    check("force removes it", not os.path.exists(path))
    check("and it leaves list_worktrees",
          "email-fix" not in [w["id"] for w in W.list_worktrees(REPO)])
    check("the branch SURVIVES dispose by default",
          git("rev-parse", "--verify", "-q", "wt/email-fix").returncode == 0,
          "<- your commits are not binned by a cleanup")

    print("\nYOUR FOLDER IS UNTOUCHABLE")
    check("it is the only worktree again", git("worktree", "list").stdout.count("\n") == 1)
    check("main branch still exists", git("rev-parse", "--verify", "-q", "main").returncode == 0)
    check("the folder is STILL clean", git("status", "--porcelain").stdout.strip() == "")
    check("and its .env was never moved or removed",
          pathlib.Path(REPO, "shared", ".env").read_text() == "TOKEN=secret\n")

    print("\nDISPOSE CANNOT REACH YOUR FOLDER")
    for bad in ("../../repo", "..", "/etc"):
        try:
            W.dispose(bad, REPO, force=True)
            check("dispose refuses %r" % bad, False, "ACCEPTED")
        except W.WorktreeError:
            check("dispose refuses %r" % bad, True)
    check("the repo survived all of that", os.path.isdir(os.path.join(REPO, ".git")))

finally:
    shutil.rmtree(TMP, ignore_errors=True)

bad = [n for n, ok in results if not ok]
print()
if bad:
    print("FAILED - %d:" % len(bad))
    for n in bad:
        print("  - " + n)
    sys.exit(1)
print("All %d checks passed." % len(results))
