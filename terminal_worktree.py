#!/usr/bin/env python3
"""
Worktree groups - one isolated checkout, several sessions living inside it.

WHAT THIS IS FOR
    You press the worktree button, give it a name, a base branch and a number,
    and get one git worktree on its own branch with that many shells already
    sitting in it. The sidebar draws them as a folder. This module owns the git
    half; terminal_manager owns the tmux half and joins the two.

WHY THE ROOT IS OUTSIDE THE FOLDER
    Worktrees live in ~/.mattdaemon-worktrees, never inside the folder they were
    cut from. Underneath it, every checkout would show up as untracked noise in
    `git status` in your actual project - and the session diff view, which reads
    exactly that, would start reporting your worktrees back to you as changes
    you had made.

WHICH REPO
    Whichever repo the caller names, defaulting to the folder the app is set to.
    The caller passes it in, which keeps this module free of an import back into
    terminal_manager, and lets one app hold worktrees of several repos at once.

    Nothing records which repo a worktree came from, because it does not need to:
    a worktree's own .git is a FILE reading `gitdir: /path/to/repo/.git/worktrees/<id>`,
    so the checkout names its owner. That is why list and dispose work across every
    repo at once without a datastore that could drift from what is on disk.

THE DANGEROUS PART
    dispose() removes directories. Every path it touches is derived from a
    validated id and re-checked against the worktree root before anything is
    deleted - an id of '../../..' must not become an rm on your home folder.
    Nothing here goes through a shell, so it does its own checking rather than
    trusting anything else to.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# MTS_WORKTREE_ROOT exists so the tests can redirect this at a temp dir.
# Nothing in the app sets it.
WORKTREE_ROOT = (os.environ.get("MTS_WORKTREE_ROOT")
                 or os.path.join(os.path.expanduser("~"), ".mattdaemon-worktrees"))

BRANCH_PREFIX = "wt/"

# Untracked files symlinked into a new checkout. `git worktree add` carries over
# TRACKED files only, so a fresh checkout of a project whose secrets live in a
# gitignored .env starts with no environment at all and every script in it fails
# on the first line that reads one. Symlink rather than copy: one source of
# truth, no second copy of the secrets on disk, and the path stays gitignored so
# the new checkout's own `git status` is clean from the first second.
#
# Deliberately config only. Runtime STATE (sqlite files, caches) is left out:
# symlinking one database into four parallel checkouts means four shells writing
# one file, which is a corruption bug waiting for a busy afternoon.
SEED_PATHS = ["shared/.env", ".env", ".env.local"]

# Deliberately strict: an id becomes both a directory name and a git branch, and
# the only ids we ever need are ones this module generated from a slug.
WT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

MAX_SESSIONS = 6


# Where to look for repos to offer in the picker. The folder the app is set to is
# always included on top of these, wherever it lives.
DEFAULT_ROOTS = ["~/Documents", "~/code", "~/dev", "~/src", "~/Projects", "~/repos", "~/git"]
DISCOVER_DEPTH = 3
_SKIP_DIRS = {"node_modules", ".venv", "venv", "vendor", "Library", "build", "dist",
              ".next", "target", "Pods", ".Trash"}

GITHUB_API = "https://api.github.com"


class WorktreeError(RuntimeError):
    pass


def _git(*args, cwd=None, timeout=180):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def is_git_repo(repo):
    if not repo or not os.path.isdir(repo):
        return False
    r = _git("rev-parse", "--show-toplevel", cwd=repo, timeout=20)
    return r.returncode == 0


def _common_dir(path):
    """The .git a checkout actually shares, so its worktrees fold onto it."""
    r = _git("rev-parse", "--git-common-dir", cwd=path, timeout=10)
    if r.returncode != 0:
        return None
    p = (r.stdout or "").strip()
    if not p:
        return None
    if not os.path.isabs(p):
        p = os.path.join(path, p)
    return os.path.realpath(p)


def remote_slug(path):
    """owner/name from origin, or "" - the only stable name a repo has.

    A folder name is whatever someone typed the day they cloned it. This Mac has
    a checkout of mlsuite whose main copy sits inside AI-Hub under
    `dev-mlsuite-build`, so naming the picker after directories would offer you
    "dev-mlsuite-build" and hide the repo you were actually looking for.
    """
    r = _git("remote", "get-url", "origin", cwd=path, timeout=10)
    if r.returncode != 0:
        return ""
    url = (r.stdout or "").strip()
    m = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    return "%s/%s" % (m.group(1), m.group(2)) if m else ""


def default_branch(repo):
    """The branch this repo calls home - main, master, whatever it actually is.

    Asked rather than assumed, because prefilling the form with the branch a
    checkout happens to be sitting on is how you quietly cut a new feature off
    somebody's half-finished one.
    """
    r = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, timeout=10)
    name = (r.stdout or "").strip()
    if r.returncode == 0 and "/" in name:
        return name.split("/", 1)[1]
    for cand in ("main", "master"):
        if _git("rev-parse", "--verify", "--quiet", cand, cwd=repo, timeout=10).returncode == 0:
            return cand
    return current_branch(repo)


def _last_commit(path):
    """{'when': '3 days ago', 'at': 1712..., 'subject': '...'} for the tip."""
    r = _git("log", "-1", "--format=%cr\t%ct\t%s", cwd=path, timeout=10)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return {"when": "", "at": 0, "subject": ""}
    parts = r.stdout.strip().splitlines()[0].split("\t", 2)
    when = parts[0].strip() if parts else ""
    try:
        at = int(parts[1])
    except (IndexError, ValueError):
        at = 0
    subject = parts[2].strip()[:90] if len(parts) > 2 else ""
    return {"when": when, "at": at, "subject": subject}


_repo_cache = {"at": 0.0, "roots": None, "repos": []}


def discover_repos(extra_roots=None, always=None, max_age=30.0):
    """Repos on this machine, ONE entry per repo. [{path, name, branch, worktrees}]

    A worktree is another checkout of a repo, not another repo - branching from
    one lands in exactly the same place as branching from its parent. This Mac
    has eight checkouts of four repos and the box has twenty of a handful, so a
    list of every directory with a .git in it would be a puzzle rather than a
    choice. Checkouts are folded onto the .git they share and the main one is
    what gets offered; the rest are counted, not listed.

    Our own worktree root is skipped outright: the folders this feature creates
    must never come back as things to create from.
    """
    roots = [os.path.expanduser(r) for r in (extra_roots or DEFAULT_ROOTS)]
    for a in (always or []):
        if a:
            roots.append(os.path.expanduser(a))
            roots.append(os.path.dirname(os.path.expanduser(a).rstrip(os.sep)))
    key = tuple(sorted(set(roots)))
    now = time.time()
    if _repo_cache["roots"] == key and (now - _repo_cache["at"]) < max_age:
        return list(_repo_cache["repos"])

    wt_root = os.path.realpath(WORKTREE_ROOT)
    seen_dirs, found = set(), {}

    def visit(d, depth):
        real = os.path.realpath(d)
        if real in seen_dirs or real == wt_root or real.startswith(wt_root + os.sep):
            return
        seen_dirs.add(real)
        if not os.path.isdir(real):
            return
        if os.path.exists(os.path.join(real, ".git")):
            common = _common_dir(real)
            if common:
                slot = found.setdefault(common, {"main": None, "seen": [], "others": 0})
                slot["seen"].append(real)
                # The main checkout is the one whose .git is a directory; every
                # worktree of it has a .git file instead.
                if os.path.isdir(os.path.join(real, ".git")):
                    slot["main"] = slot["main"] or real
                else:
                    slot["others"] += 1
                return          # never descend into a checkout
        if depth <= 0:
            return
        try:
            entries = os.listdir(real)
        except OSError:
            return
        for name in entries:
            if name.startswith(".") or name in _SKIP_DIRS:
                continue
            visit(os.path.join(real, name), depth - 1)

    for r in roots:
        visit(r, DISCOVER_DEPTH)

    out = []
    for common, slot in found.items():
        # Prefer the main checkout, then any checkout actually seen, and only
        # then the folder beside the shared .git - which is a real place but not
        # always one the user has ever opened.
        path = slot["main"] or (sorted(slot["seen"], key=len)[0] if slot["seen"]
                                else os.path.dirname(common.rstrip(os.sep)))
        if not os.path.isdir(path):
            continue
        slug = remote_slug(path)
        out.append({
            "path": path,
            "name": slug.split("/")[-1] if slug else os.path.basename(path.rstrip(os.sep)),
            "slug": slug,
            "branch": current_branch(path),
            "default_branch": default_branch(path),
            "worktrees": slot["others"],
            # What the repo IS, in the two facts that actually tell them apart:
            # when it last moved, and what the last thing done to it was. A name
            # on its own is a guess.
            "last": _last_commit(path),
        })
    # Several clones of one repo are several repos on disk and each is a real
    # choice - but "mlsuite" listed four times is not a choice, it is a coin
    # toss. So the one in active use keeps the plain name and the rest say which
    # folder they are. Active use = most worktrees hanging off it, then most
    # recently committed to; that is the working copy on any machine.
    by_name = {}
    for r in out:
        by_name.setdefault(r["name"], []).append(r)
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (-r["worktrees"], -(r["last"] or {}).get("at", 0)))
        for r in group[1:]:
            leaf = os.path.basename(r["path"].rstrip(os.sep))
            if leaf != r["name"]:
                r["name"] = "%s (%s)" % (r["name"], leaf)

    out.sort(key=lambda r: r["name"].lower())
    _repo_cache.update({"at": now, "roots": key, "repos": out})
    return list(out)


# ── GitHub ──
# Only ever used to LIST what you could clone and to clone it. The token stays in
# this process: it is read from the OS credential store, used server-side, and
# never sent to the page - the same rule the app already applies to the VPS token.

GH_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def github_token():
    """The token git already uses for github.com, or "" if there is none.

    Read from the credential helper rather than asked for: pushes from this
    machine already authenticate, so there is a working token there and no
    reason to make anyone create a second one.
    """
    env = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env:
        return env.strip()
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(
            ["git", "-c", "credential.helper=osxkeychain", "credential", "fill"],
            input="host=github.com\nprotocol=https\n\n",
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def github_repos(limit=100):
    """Your repos, most recently touched first. Raises if there is no token."""
    tok = github_token()
    if not tok:
        raise WorktreeError(
            "No GitHub token on this machine. Pushes normally leave one in the "
            "keychain - sign in to GitHub from git once, and this fills itself in.")
    url = ("%s/user/repos?per_page=%d&sort=updated&affiliation=owner,collaborator,"
           "organization_member" % (GITHUB_API, max(1, min(int(limit), 100))))
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "User-Agent": "mattdaemon-worktrees",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise WorktreeError("GitHub said %s. The token may not have repo access." % e.code)
    except Exception as e:
        raise WorktreeError("Could not reach GitHub: %s" % e)
    if not isinstance(data, list):
        raise WorktreeError("Unexpected answer from GitHub")
    return [{
        "full_name": d.get("full_name", ""),
        "name": d.get("name", ""),
        "private": bool(d.get("private")),
        "default_branch": d.get("default_branch", ""),
        "updated_at": d.get("updated_at", ""),
    } for d in data if d.get("full_name")]


def clone(full_name, dest_parent, name=None):
    """Clone one repo into dest_parent. Returns the new checkout's path.

    The URL is built here from a validated owner/name rather than taken from the
    caller, so nothing else can decide what gets fetched or where it lands. The
    token is NOT put in the URL - that would write it into .git/config, where it
    would sit in plain text long after this call. Git's own credential helper
    does the authenticating, exactly as it does for your pushes.
    """
    if not isinstance(full_name, str) or not GH_REPO_RE.match(full_name.strip()):
        raise WorktreeError("invalid repository %r - expected owner/name" % (full_name,))
    full_name = full_name.strip()
    parent = os.path.realpath(os.path.expanduser(dest_parent or ""))
    if not os.path.isdir(parent):
        raise WorktreeError("no such folder: %s" % parent)
    if os.path.realpath(WORKTREE_ROOT) == parent or parent.startswith(
            os.path.realpath(WORKTREE_ROOT) + os.sep):
        raise WorktreeError("that folder holds worktrees - clone somewhere else")

    leaf = (name or full_name.split("/", 1)[1]).strip()
    if not re.match(r"^[A-Za-z0-9_.-]{1,100}$", leaf) or leaf in (".", ".."):
        raise WorktreeError("invalid folder name %r" % (leaf,))
    dest = os.path.join(parent, leaf)
    if not os.path.realpath(dest).startswith(parent + os.sep):
        raise WorktreeError("refusing a path outside %s" % parent)
    if os.path.exists(dest):
        raise WorktreeError("%s already exists" % dest)

    url = "https://github.com/%s.git" % full_name
    args = ["clone", url, dest]
    if sys.platform == "darwin":
        args = ["-c", "credential.helper=osxkeychain"] + args
    r = _git(*args, cwd=parent, timeout=900)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        # Never echo a URL that could carry a credential back to the browser.
        raise WorktreeError("git clone failed: %s" % err.replace(url, full_name))
    _repo_cache["at"] = 0.0        # the picker must see it immediately
    return dest


def validate_id(wt_id):
    if not isinstance(wt_id, str) or not WT_ID_RE.match(wt_id):
        raise WorktreeError(
            "invalid worktree id %r - must match %s. Rejected before it can "
            "become a path or a branch name." % (wt_id, WT_ID_RE.pattern))
    return wt_id


def validate_base(base):
    """A base ref is typed by a person and lands in a git argv slot.

    It cannot become a shell injection - no shell is involved - but a value like
    '--force' would be read by git as a FLAG rather than a commit-ish. Refusing a
    leading dash is the whole guard.
    """
    if not isinstance(base, str) or not base.strip():
        raise WorktreeError("base branch is required")
    base = base.strip()
    if base.startswith("-"):
        raise WorktreeError("invalid base %r - a ref cannot start with a dash" % base)
    if len(base) > 200 or any(c in base for c in "\n\r\t "):
        raise WorktreeError("invalid base %r" % base)
    return base


def slugify(name):
    """A human name becomes the id. Anything not [a-z0-9] becomes a dash."""
    if not isinstance(name, str):
        raise WorktreeError("worktree name must be text")
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:48].strip("-")
    if not slug:
        raise WorktreeError(
            "worktree name %r has no usable characters - give it a name with "
            "letters or numbers in it." % (name,))
    return validate_id(slug)


def worktree_path(wt_id):
    validate_id(wt_id)
    path = os.path.join(WORKTREE_ROOT, wt_id)
    # Belt and braces: even a validated id gets re-checked against the root, so a
    # future loosening of the regex cannot quietly become a path escape.
    real_root = os.path.realpath(WORKTREE_ROOT)
    real_path = os.path.realpath(path)
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        raise WorktreeError("refusing a path outside %s: %r" % (WORKTREE_ROOT, real_path))
    return path


def branch_name(wt_id):
    return BRANCH_PREFIX + validate_id(wt_id)


def _branch_exists(branch, repo):
    r = _git("rev-parse", "--verify", "--quiet", "refs/heads/" + branch,
             cwd=repo, timeout=20)
    return r.returncode == 0


def current_branch(repo):
    """The branch the folder is on, for prefilling the base field."""
    if not is_git_repo(repo):
        return ""
    r = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo, timeout=20)
    name = (r.stdout or "").strip()
    return name if r.returncode == 0 and name and name != "HEAD" else "HEAD"


def unique_id(name, repo):
    """Slug of the name, suffixed until it collides with nothing.

    Checks the directory AND the branch. dispose() keeps branches on purpose, so
    a leftover branch from a removed worktree would otherwise make the next one
    of the same name fail on `git worktree add -b`.
    """
    base = slugify(name)
    candidate = base
    n = 1
    while os.path.exists(os.path.join(WORKTREE_ROOT, candidate)) or \
            _branch_exists(BRANCH_PREFIX + candidate, repo):
        n += 1
        candidate = "%s-%d" % (base[:44].rstrip("-"), n)
        if n > 99:
            raise WorktreeError("too many worktrees named like %r" % base)
    return validate_id(candidate)


def seed(wt_id, repo):
    """Symlink in the untracked config a checkout needs. Returns what was linked.

    Never overwrites: a path already present (tracked, or linked by an earlier
    call) is left alone. Every destination is re-checked to be inside the
    worktree, so a SEED_PATHS entry can never write outside it.
    """
    path = worktree_path(wt_id)
    real_wt = os.path.realpath(path)
    linked = []
    for rel in SEED_PATHS:
        src = os.path.join(repo, rel)
        if not os.path.exists(src) or os.path.isdir(src):
            continue
        dest = os.path.join(path, rel)
        if not os.path.realpath(dest).startswith(real_wt + os.sep):
            continue
        if os.path.exists(dest) or os.path.islink(dest):
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.symlink(os.path.realpath(src), dest)
            linked.append(rel)
        except OSError:
            # A seed failure is not worth losing the worktree over. The shells
            # still work; they just start without that one file.
            pass
    return linked


def create(name, base, repo):
    """One isolated checkout on its own branch. Returns {id, name, branch, path}."""
    if not is_git_repo(repo):
        raise WorktreeError(
            "%s is not a git repository, so there is nothing to branch from. "
            "Point the app at a repo in settings first." % (repo or "the folder"))
    base = validate_base(base)
    wt_id = unique_id(name, repo)
    path = worktree_path(wt_id)
    branch = branch_name(wt_id)

    if os.path.exists(path):
        raise WorktreeError("worktree already exists: %s" % path)
    os.makedirs(WORKTREE_ROOT, exist_ok=True)

    r = _git("worktree", "add", "-b", branch, path, base, cwd=repo)
    if r.returncode != 0:
        raise WorktreeError("git worktree add failed: %s"
                            % (r.stderr or r.stdout).strip())

    return {
        "id": wt_id,
        "name": (name.strip() if isinstance(name, str) and name.strip() else wt_id),
        "branch": branch,
        "path": path,
        "base": base,
        "repo": repo,
        "seeded": seed(wt_id, repo),
    }


def is_dirty(wt_id):
    """True when the checkout holds uncommitted work worth warning about.

    Deliberately not called from the sidebar's poll: `git status` on a large
    checkout is cheap once and expensive forty times a minute. Removal asks at
    the one moment the answer matters.
    """
    path = worktree_path(wt_id)
    if not os.path.isdir(path):
        return False
    r = _git("status", "--porcelain", cwd=path, timeout=60)
    if r.returncode != 0:
        # Cannot tell - answer "dirty" so the guard errs towards keeping work.
        return True
    return bool((r.stdout or "").strip())


def owner_repo(wt_id):
    """Which repo a worktree was cut from, read from the checkout itself.

    A worktree's .git is a FILE, not a directory, and it holds
    `gitdir: /path/to/repo/.git/worktrees/<id>`. So the checkout carries its own
    provenance and nothing has to remember it - which is what lets one app hold
    worktrees of several repos without a list that can go stale.

    Returns None for a directory that is not a worktree any more.
    """
    path = worktree_path(wt_id)
    dotgit = os.path.join(path, ".git")
    if not os.path.isfile(dotgit):
        return None
    try:
        with open(dotgit, "r") as fh:
            line = fh.read(4096).strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    marker = os.sep + ".git" + os.sep + "worktrees" + os.sep
    if marker in gitdir:
        return gitdir.split(marker)[0]
    # A bare repo keeps worktrees under <repo>/worktrees/<id> with no .git level.
    marker2 = os.sep + "worktrees" + os.sep
    if marker2 in gitdir:
        return gitdir.split(marker2)[0]
    return None


def list_worktrees(repo=None, with_dirty=False):
    """Every live worktree, whichever repo it came from.

    Scans the root rather than asking one repo, because `git worktree list`
    answers for a single repo and the whole point here is that several can be in
    play at once. Pass `repo` to narrow it to one.
    """
    root = WORKTREE_ROOT
    if not os.path.isdir(root):
        return []
    want = os.path.realpath(repo) if repo else None
    out = []
    for wt_id in sorted(os.listdir(root)):
        if not WT_ID_RE.match(wt_id):
            continue        # something else living under the root; not ours
        path = os.path.join(root, wt_id)
        if not os.path.isdir(path):
            continue
        owner = owner_repo(wt_id)
        if want and (not owner or os.path.realpath(owner) != want):
            continue
        item = {"id": wt_id, "branch": branch_name(wt_id),
                "path": os.path.realpath(path), "repo": owner or "",
                "repo_name": os.path.basename(owner.rstrip(os.sep)) if owner else ""}
        if with_dirty:
            item["dirty"] = is_dirty(wt_id)
        out.append(item)
    return out


def exists(wt_id):
    try:
        return os.path.isdir(worktree_path(wt_id))
    except WorktreeError:
        return False


def dispose(wt_id, repo=None, keep_branch=True, force=False):
    """Remove a worktree. Never touches your folder or the branch it is on.

    force is what the second confirm asks for. The default refusal is the point:
    a checkout with uncommitted work is the only copy of that work.

    keep_branch defaults to TRUE. The checkout is scratch and rebuildable; the
    branch holds commits. Pruning dead branches is a decision, not a side effect
    of closing a group of terminals.
    """
    path = worktree_path(wt_id)          # validates + re-checks the root
    branch = branch_name(wt_id)
    # Ask the checkout which repo owns it rather than trusting the caller to
    # remember: `git worktree remove` only works from the repo it is registered
    # to, and handing it the wrong one silently leaves the worktree registered.
    repo = owner_repo(wt_id) or repo

    if repo and os.path.realpath(path) == os.path.realpath(repo):
        raise WorktreeError("refusing to dispose of the folder itself")

    if not force and is_dirty(wt_id):
        raise WorktreeError(
            "worktree %s has uncommitted changes. Commit them, or remove it "
            "again with force to throw them away." % wt_id)

    if is_git_repo(repo):
        _git("worktree", "remove", "--force", path, cwd=repo)
    # `git worktree remove` leaves the directory behind if it was never
    # registered (a half-created worktree). Clean up only inside the root.
    if os.path.isdir(path):
        real_root = os.path.realpath(WORKTREE_ROOT)
        if os.path.realpath(path).startswith(real_root + os.sep):
            shutil.rmtree(path, ignore_errors=True)
    if is_git_repo(repo):
        _git("worktree", "prune", cwd=repo)
        if not keep_branch and branch.startswith(BRANCH_PREFIX):
            # Only ever reached when a caller explicitly asks, and prefix-guarded
            # so it can never resolve to anything but wt/*.
            _git("branch", "-D", branch, cwd=repo)
    return True
