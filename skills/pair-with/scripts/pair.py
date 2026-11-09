#!/usr/bin/env python3
"""Coordination helper for the pair-with skill. Python 3.11+, stdlib only.

Every command prints one JSON object on stdout.

Exit codes:
  0  ok
  1  request refused, or a check failed
  2  bad settings, usage, or setup
  3  test machine unavailable (item is held and retried later)
  4  test machine low on disk (item goes to needs_you)
  5  rebase conflict, or main moved: resolve if needed, then run merge again
  6  main could not be updated (item goes to needs_you)
  7  an earlier run of this command is still going: try again later

Live state lives in <git common dir>/backlog-duo/, never in tracked files, so
claiming an item needs no commit and survives across worktrees. What happened, and
when, is appended to events.jsonl there; `report` summarizes it.
"""
import argparse
import contextlib
import fcntl
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

BACKLOG_DIR = "docs/backlog"
# These keep the skill's old name so existing repos and their runs keep working.
SETTINGS_FILE = ".backlog-duo.toml"
WORKTREE_DIR = ".backlog-duo-worktrees"
REMOTE_REF = "refs/heads/backlog-duo-test"
MARKER = ".git/backlog-duo-checkout"
ACTIVE = {"in_progress", "needs_review", "reviewing"}
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}   # No priority counts as P3.
PLAN_PREFIX = "plan-batch-"
RUN_PID_FILE = ".git/backlog-duo-run.pid"
# No connection sharing: a background ssh master would inherit and hold our locks.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=no", "-o", "ControlPath=none"]
HOLD_SECONDS = 1800
POLL_SECONDS = 10
LOG_TAIL_LINES = 40
EXIT_RUN_ACTIVE = 75
EXIT_NOT_SET_UP = 78

LOCK_FDS = set()       # Locks children inherit, so they outlive a killed helper.
CHILD_GROUPS = set()   # Process groups to stop if this helper is told to exit.


class Refused(Exception):
    def __init__(self, message, code=1, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def emit(obj, code=0):
    print(json.dumps(obj, indent=2))
    sys.exit(code)


def run(cmd, cwd=None, env=None):
    return subprocess.run(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True
    )


def git(*args, cwd=None):
    """Run git and return stdout; raise Refused on failure."""
    proc = run(["git", *args], cwd=cwd)
    if proc.returncode:
        raise Refused(f"git {' '.join(args)}: {proc.stderr.strip()}", code=2)
    return proc.stdout.strip()


def git_ok(*args, cwd=None):
    return run(["git", *args], cwd=cwd).returncode == 0


@contextlib.contextmanager
def file_lock(path, timeout=None, inherit=False):
    """Hold an flock on path. The kernel frees it once every process holding it is gone.

    With inherit=True, child processes keep the lock alive, so a retry can't start
    while a check from a killed helper is still running.
    """
    with open(path, "a") as handle:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                flags = fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB)
                fcntl.flock(handle, flags)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(str(path))
                time.sleep(min(POLL_SECONDS, 1))
        if inherit:
            LOCK_FDS.add(handle.fileno())
        try:
            yield
        finally:
            LOCK_FDS.discard(handle.fileno())


def path_matches(path, pattern):
    """fnmatch semantics ('*' crosses '/'), plus a bare directory matches its contents."""
    return fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/")


def literal_prefix(pattern):
    match = re.search(r"[*?\[]", pattern)
    return pattern[: match.start()] if match else pattern


def areas_overlap(left, right):
    """Conservative: a false positive only makes two items run one after the other."""
    for a in left:
        for b in right:
            pa, pb = literal_prefix(a), literal_prefix(b)
            if pa.startswith(pb) or pb.startswith(pa):
                return True
    return False


class Ctx:
    def __init__(self):
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
        self.common = Path(common)
        self.refresh()
        self.root = Path(self.worktrees[0]["path"])
        self.state_dir = self.common / "backlog-duo"
        path = self.root / SETTINGS_FILE
        if not path.exists():
            raise Refused(f"no {SETTINGS_FILE} in {self.root}: run first-time setup", code=2)
        try:
            self.settings = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as err:
            raise Refused(f"{SETTINGS_FILE}: {err}", code=2)
        self.main = self.settings.get("main_branch", "main")
        self.agents = self.settings.get("agents", {})
        if len(self.agents) != 2:
            raise Refused(f"{SETTINGS_FILE} must define exactly two agents", code=2)
        names = [check.get("name") for check in self.settings.get("checks", [])]
        for check in self.settings.get("checks", []):
            if not (check.get("name") and check.get("run") and check.get("when")):
                raise Refused(f"every check needs name, run and when: {check}", code=2)
        if len(names) != len(set(names)):
            raise Refused(f"check names must be unique: {names}", code=2)

    def refresh(self):
        self.worktrees = parse_worktrees(git("worktree", "list", "--porcelain"))

    def lock_path(self, name):
        self.ensure_dirs()
        return self.state_dir / f"{name}.lock"

    def ensure_dirs(self):
        (self.state_dir / "logs").mkdir(parents=True, exist_ok=True)

    def main_worktree(self):
        for tree in self.worktrees:
            if tree.get("branch") == f"refs/heads/{self.main}":
                return tree["path"]
        return None

    def require_agent(self, name):
        if name not in self.agents:
            raise Refused(f"unknown agent {name!r}; settings define {sorted(self.agents)}", code=2)


def parse_worktrees(text):
    trees, current = [], {}
    for line in text.splitlines():
        if not line:
            if current:
                trees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[{"worktree": "path"}.get(key, key)] = value
    if current:
        trees.append(current)
    return trees


@contextlib.contextmanager
def state(ctx):
    """Read-modify-write the state file under its lock. Skips the save on exception."""
    path = ctx.state_dir / "state.json"
    with file_lock(ctx.lock_path("state")):
        data = {"items": {}, "merged": [], "seen": {}, "waiting_since": {}}
        if path.exists():
            data = json.loads(path.read_text())
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)


def log_event(ctx, event, **fields):
    """Append one event to the run's history. History is best effort: it never blocks work."""
    with contextlib.suppress(OSError):
        ctx.ensure_dirs()
        with open(ctx.state_dir / "events.jsonl", "a") as out:
            out.write(json.dumps({"at": time.time(), "event": event, **fields}) + "\n")


def excluded_from_report(ctx, path):
    return any(path_matches(path, p) for p in ctx.settings.get("report_exclude", []))


def work_stats(ctx, cwd, diff_args, commits):
    """Commits and changed lines, leaving out binary files and report_exclude paths."""
    files = lines = 0
    for row in git("diff", "--no-renames", "--numstat", *diff_args, cwd=cwd).splitlines():
        added, deleted, path = row.split("\t", 2)
        if added == "-" or excluded_from_report(ctx, path):
            continue
        files += 1
        lines += int(added) + int(deleted)
    return {"commits": commits, "files": files, "lines": lines}


def author_work(ctx, branch):
    commits = int(git("rev-list", "--count", f"{ctx.main}..{branch}", cwd=ctx.root))
    return work_stats(ctx, ctx.root, [f"{ctx.main}...{branch}"], commits)


def review_work(ctx, item, worktree):
    """What the reviewer changed since it took the item over."""
    base = item.get("review_base")
    if not base or not git_ok("cat-file", "-e", f"{base}^{{commit}}", cwd=worktree):
        return None
    if git_ok("merge-base", "--is-ancestor", base, "HEAD", cwd=worktree):
        commits = int(git("rev-list", "--count", f"{base}..HEAD", cwd=worktree))
        return work_stats(ctx, worktree, [base, "HEAD"], commits)
    # The reviewer rebased, so main's changes are in the range too. Count only the
    # branch's own files; that's close, not exact.
    paths = git("diff", "--no-renames", "--name-only", f"{ctx.main}...HEAD", cwd=worktree).splitlines()
    total = int(git("rev-list", "--count", f"{ctx.main}..HEAD", cwd=worktree))
    authored = (item.get("author_work") or {}).get("commits", 0)
    if not paths:
        return {"commits": max(0, total - authored), "files": 0, "lines": 0}
    return work_stats(ctx, worktree, [base, "HEAD", "--", *paths], max(0, total - authored))


def split_list(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_item(slug, text):
    item = {"slug": slug, "worth": None, "touches": [], "depends": [], "added": "", "title": slug}
    lines = text.splitlines()
    body = lines
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body = lines[index + 1 :]
                break
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key in ("touches", "depends"):
                item[key] = split_list(value)
            elif key in ("worth", "added", "where", "issue", "priority"):
                item[key] = value
    for line in body:
        if line.startswith("# "):
            item["title"] = line[2:].strip()
            break
    return item


def read_backlog(ctx):
    """Items as committed on main. Uncommitted item files are deliberately invisible."""
    listing = git("ls-tree", "--name-only", ctx.main, f"{BACKLOG_DIR}/", cwd=ctx.root)
    items = {}
    for path in listing.splitlines():
        if not path.endswith(".md"):
            continue
        slug = Path(path).stem
        items[slug] = parse_item(slug, git("show", f"{ctx.main}:{path}", cwd=ctx.root))
    return items


def item_problem(ctx, item, on_main):
    """Why an item can never be claimed without help from the user, if anything."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item["slug"]):
        return "slug must use lowercase letters, digits and hyphens"
    if not item["touches"]:
        return "no touches list, so overlap cannot be judged"
    if item.get("priority") and item["priority"] not in PRIORITIES:
        return f"priority must be one of {', '.join(PRIORITIES)}, not {item['priority']!r}"
    for dep in item["depends"]:
        if dep not in on_main and not git(
            "log", "-1", "--format=%h", ctx.main, "--", f"{BACKLOG_DIR}/{dep}.md", cwd=ctx.root
        ):
            return f"depends on unknown item {dep!r}"
    return None


def on_main_tip(ctx, sha):
    return git_ok("merge-base", "--is-ancestor", sha, ctx.main, cwd=ctx.root)


def finish_merge(ctx, st, slug, sha):
    """Clean up after an item landed on main. Safe to run more than once."""
    item = st["items"].pop(slug)
    warnings = []
    ctx.refresh()
    if any(tree["path"] == item["worktree"] for tree in ctx.worktrees):
        if not git_ok("worktree", "remove", item["worktree"], cwd=ctx.root):
            warnings.append(f"left worktree in place: {item['worktree']}")
    ref = f"refs/heads/{item['branch']}"
    if git_ok("rev-parse", "--verify", "--quiet", ref, cwd=ctx.root) and not warnings:
        if on_main_tip(ctx, ref):
            git_ok("branch", "-D", item["branch"], cwd=ctx.root)
        else:
            warnings.append(f"left branch in place: {item['branch']}")
    st["merged"].append({"item": slug, "sha": sha, "author": item["author"],
                         "reviewer": item.get("reviewer"), "issue": item.get("issue"),
                         "at": time.time(),
                         "claimed_at": item.get("claimed_at"),
                         "submitted_at": item.get("submitted_at"),
                         "review_started_at": item.get("review_started_at"),
                         "author_work": item.get("author_work"),
                         "review_work": item.get("review_work"),
                         "warnings": warnings})
    log_event(ctx, "merge", item=slug, author=item["author"], reviewer=item.get("reviewer"), sha=sha)


def reconcile(ctx, st):
    """Bring state in line with git after an interruption or a change made outside the run."""
    ctx.refresh()
    on_main = read_backlog(ctx)
    for slug, item in list(st["items"].items()):
        sha = item.get("merging")
        if sha and on_main_tip(ctx, sha):
            finish_merge(ctx, st, slug, sha)
            continue
        if item["status"] == "needs_you":
            continue
        branch_there = git_ok("rev-parse", "--verify", "--quiet",
                              f"refs/heads/{item['branch']}", cwd=ctx.root)
        if slug not in on_main:
            item.update(status="needs_you", reviewer=None,
                        reason="item file was removed from main outside this run")
        elif not branch_there:
            item.update(status="needs_you", reviewer=None,
                        reason=f"branch {item['branch']} no longer exists")
        elif not Path(item["worktree"]).is_dir():
            git("worktree", "prune", cwd=ctx.root)
            ctx.refresh()
            create_worktree(ctx, slug)
            ctx.refresh()


def effective_ranks(on_main):
    """Each item's priority rank, raised to that of anything that depends on it, transitively."""
    rank = {slug: PRIORITIES.get(item.get("priority") or "P3", 3) for slug, item in on_main.items()}
    changed = True
    while changed:
        changed = False
        for slug, item in on_main.items():
            if item["worth"] != "yes":
                continue
            for dep in item["depends"]:
                if dep in rank and rank[slug] < rank[dep]:
                    rank[dep] = rank[slug]
                    changed = True
    return rank


def held_by_pause(st, slug):
    paused = st.get("paused")
    return bool(paused) and (paused.get("now") or slug.startswith(PLAN_PREFIX))


def areas(item):
    return item.get("touches", []) + item.get("changed", [])


def create_worktree(ctx, slug):
    branch, path = f"duo/{slug}", ctx.root / WORKTREE_DIR / slug
    if not any(tree["path"] == str(path) for tree in ctx.worktrees):
        exclude = ctx.common / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if f"/{WORKTREE_DIR}/" not in existing:
            exclude.write_text(existing + f"/{WORKTREE_DIR}/\n")
        if git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=ctx.root):
            git("worktree", "add", "--quiet", str(path), branch, cwd=ctx.root)
        else:
            git("worktree", "add", "--quiet", "-b", branch, str(path), ctx.main, cwd=ctx.root)
    return branch, str(path)


def item_view(ctx, slug, item, action):
    return {
        "action": action,
        "item": slug,
        "file": f"{BACKLOG_DIR}/{slug}.md",
        "branch": item["branch"],
        "worktree": item["worktree"],
        "author": item["author"],
        "issue": item.get("issue"),
        "main": ctx.main,
        "off_limits": ctx.settings.get("off_limits", []),
        "repo_notes": ctx.settings.get("repo_notes", ""),
    }


def choose_next(ctx, agent, st):
    now = time.time()
    st["seen"][agent] = now
    reconcile(ctx, st)
    items = st["items"]

    for slug, item in items.items():
        if item["status"] == "in_progress" and item["author"] == agent:
            return item_view(ctx, slug, item, "implement")
        if item["status"] == "reviewing" and item["reviewer"] == agent:
            return item_view(ctx, slug, item, "review")

    waiting = [
        (item["submitted_at"], slug)
        for slug, item in items.items()
        if item["status"] == "needs_review"
        and item["author"] != agent
        and item.get("hold_until", 0) <= now
    ]
    if waiting:
        slug = min(waiting)[1]
        item = items[slug]
        item.update(status="reviewing", reviewer=agent)
        if "review_base" not in item:
            # The first review start fixes what the author built, for the report.
            item["review_base"] = git("rev-parse", f"refs/heads/{item['branch']}", cwd=ctx.root)
            item["author_work"] = author_work(ctx, item["branch"])
            item["review_started_at"] = now
        log_event(ctx, "review", item=slug, agent=agent, author=item["author"])
        return item_view(ctx, slug, item, "review")

    on_main = read_backlog(ctx)
    attention, blocked = {}, {}
    unreviewed = sum(
        1 for i in items.values() if i["author"] == agent and i["status"] in ("needs_review", "reviewing")
    )
    capped = unreviewed >= ctx.settings.get("max_unreviewed", 2)
    active = [i for i in items.values() if i["status"] in ACTIVE or i["status"] == "needs_you"]
    rank = effective_ranks(on_main)
    candidates = sorted(on_main.values(), key=lambda i: (rank[i["slug"]], i["added"], i["slug"]))
    for candidate in candidates:
        slug = candidate["slug"]
        if slug in items or candidate["worth"] != "yes":
            continue
        problem = item_problem(ctx, candidate, on_main)
        if problem:
            attention[slug] = problem
            continue
        if held_by_pause(st, slug):
            blocked[slug] = "run paused for re-prioritizing"
            continue
        unmet = [dep for dep in candidate["depends"] if dep in on_main]
        if unmet:
            blocked[slug] = f"waiting for {', '.join(unmet)}"
            continue
        if any(areas_overlap(candidate["touches"], areas(other)) for other in active):
            blocked[slug] = "overlaps active work"
            continue
        if capped:
            blocked[slug] = f"{agent} already has {unreviewed} items awaiting review"
            continue
        branch, path = create_worktree(ctx, slug)
        items[slug] = {
            "status": "in_progress",
            "author": agent,
            "reviewer": None,
            "branch": branch,
            "worktree": path,
            "touches": candidate["touches"],
            "issue": candidate.get("issue"),
            "changed": [],
            "checks": {},
            "claimed_at": now,
        }
        log_event(ctx, "claim", item=slug, agent=agent)
        return item_view(ctx, slug, items[slug], "implement")

    busy = [i for i in items.values() if i["status"] in ACTIVE]
    if not busy:
        reason = ("run paused for re-prioritizing; the user resumes it" if st.get("paused")
                  else "nothing left to claim or review")
        return {"action": "stop", "reason": reason, "blocked": blocked, "attention": attention}
    since = st["waiting_since"].setdefault(agent, now)
    if now - since > ctx.settings.get("max_wait_minutes", 120) * 60:
        return {"action": "stop", "reason": "waited too long for the other agent",
                "blocked": blocked, "attention": attention}
    return {"action": "wait", "blocked": blocked, "attention": attention}


def cmd_next(ctx, args):
    ctx.require_agent(args.agent)
    deadline = time.monotonic() + args.wait_seconds
    while True:
        with state(ctx) as st:
            answer = choose_next(ctx, args.agent, st)
            if answer["action"] != "wait":
                since = st["waiting_since"].pop(args.agent, None)
                if since:
                    log_event(ctx, "wait", agent=args.agent, seconds=round(time.time() - since),
                           ended=answer["action"])
        if answer["action"] != "wait" or time.monotonic() >= deadline:
            emit(answer)
        time.sleep(min(POLL_SECONDS, max(0.1, deadline - time.monotonic())))


def changed_files(ctx, branch):
    # --no-renames: a rename must count as both paths, or moving code out of a
    # checked area into an exempt one would look like it needs no check.
    out = git("diff", "--no-renames", "--name-only", f"{ctx.main}...{branch}", cwd=ctx.root)
    return [p for p in out.splitlines() if p]


def require_clean(worktree):
    dirty = git("status", "--porcelain", cwd=worktree)
    if dirty:
        raise Refused("worktree has uncommitted changes", dirty=dirty.splitlines())


def require_item(st, slug):
    if slug not in st["items"]:
        raise Refused(f"{slug} is not an active item")
    return st["items"][slug]


def cmd_submit(ctx, args):
    ctx.require_agent(args.agent)
    with state(ctx) as st:
        item = require_item(st, args.slug)
        if item["status"] != "in_progress" or item["author"] != args.agent:
            raise Refused(f"{args.slug} is {item['status']} by {item['author']}, not yours to submit")
        require_clean(item["worktree"])
        if git("rev-list", "--count", f"{ctx.main}..{item['branch']}", cwd=ctx.root) == "0":
            raise Refused("branch has no commits; release the item instead")
        changed = changed_files(ctx, item["branch"])
        undeclared = [p for p in changed if not any(path_matches(p, t) for t in item["touches"])]
        item.update(status="needs_review", changed=changed, submitted_at=time.time())
    log_event(ctx, "submit", item=args.slug, agent=args.agent, files=len(changed))
    emit({"ok": True, "item": args.slug, "changed": changed, "undeclared": undeclared})


def cmd_release(ctx, args):
    with state(ctx) as st:
        item = require_item(st, args.slug)
        if args.agent:
            ctx.require_agent(args.agent)
        if item["status"] == "reviewing" and (args.force or item["reviewer"] == args.agent):
            item.update(status="needs_review", reviewer=None, release_reason=args.reason)
            result = "needs_review"
        elif item["status"] == "in_progress" and (args.force or item["author"] == args.agent):
            del st["items"][args.slug]
            result = "open"
        else:
            raise Refused(f"{args.slug} is {item['status']}; not releasable by you")
    log_event(ctx, "release", item=args.slug, agent=args.agent, now=result, reason=args.reason)
    emit({"ok": True, "item": args.slug, "now": result, "reason": args.reason})


def cmd_escalate(ctx, args):
    ctx.require_agent(args.agent)
    with state(ctx) as st:
        item = require_item(st, args.slug)
        if args.agent not in (item["author"], item["reviewer"]):
            raise Refused(f"{args.agent} does not hold {args.slug}")
        item.update(status="needs_you", reason=args.reason, reviewer=None)
    log_event(ctx, "escalate", item=args.slug, agent=args.agent, reason=args.reason)
    emit({"ok": True, "item": args.slug, "reason": args.reason})


def cmd_requeue(ctx, args):
    """For the user, after sorting out an item that was marked needs_you."""
    with state(ctx) as st:
        item = require_item(st, args.slug)
        if item["status"] != "needs_you":
            raise Refused(f"{args.slug} is {item['status']}, not needs_you")
        if args.to == "open":
            del st["items"][args.slug]
        else:
            item.update(status="needs_review", reviewer=None, hold_until=0,
                        submitted_at=time.time())
            item.pop("reason", None)
    log_event(ctx, "requeue", item=args.slug, now=args.to)
    emit({"ok": True, "item": args.slug, "now": args.to})


def remote_path(path):
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def ssh_cmd(host, script, tty=False):
    return ["ssh", *SSH_OPTS, *(["-tt"] if tty else []), host, script]


def ssh(host, script, timeout=None):
    return run_group(ssh_cmd(host, script), timeout=timeout)


def run_group(cmd, cwd=None, timeout=None, stdout=None):
    """Run in its own process group, stopped if this helper is told to exit.

    Holds LOCK_FDS open in the child, so if this helper is killed outright the
    locks stay held until the child ends. Long runs go through bounded() so that
    an orphan can't hold them forever.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        pass_fds=tuple(LOCK_FDS),
        stdout=stdout if stdout else subprocess.PIPE,
        stderr=subprocess.STDOUT if stdout else subprocess.PIPE,
    )
    CHILD_GROUPS.add(proc.pid)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(cmd, 124, out, err)
    finally:
        CHILD_GROUPS.discard(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def bounded(check, cmd):
    """Enforce a check's time limit in a process that survives this helper."""
    return ["timeout", "--kill-after=10", f"{check.get('timeout_minutes', 30) * 60}s", *cmd]


def stop_children(signum, _frame):
    for group in list(CHILD_GROUPS):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGTERM)
    sys.exit(128 + signum)


def tail(path):
    return Path(path).read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]


def run_local_check(check, worktree, log):
    with open(log, "w") as out:
        return run_group(bounded(check, ["bash", "-c", check["run"]]), cwd=worktree,
                         stdout=out).returncode


def machine_preflight(machine):
    """Return (exit_code, message); exit_code 0 means ready."""
    host, checkout = machine["ssh"], remote_path(machine["checkout"])
    proc = ssh(host, f"cd {checkout} && test -f {MARKER} && df -Pk . | tail -1 | awk '{{print $4}}'",
               timeout=30)
    if proc.returncode == 255 or proc.returncode == 124:
        return 3, f"cannot reach {host}"
    if proc.returncode:
        return 2, f"{machine['checkout']} on {host} is not set up: run setup-machine"
    free_gb = int(proc.stdout.split()[-1]) / (1024 * 1024)
    if free_gb < machine.get("min_free_gb", 3):
        return 4, f"{host} has {free_gb:.1f} GB free, needs {machine.get('min_free_gb', 3)} GB"
    return 0, ""


def run_machine_check(ctx, check, worktree, sha, log):
    """Return (exit_code, message). Runs one at a time per machine."""
    name = check["machine"]
    machine = ctx.settings.get("machines", {}).get(name)
    if not machine:
        return 2, f"check {check['name']} uses undefined machine {name!r}"
    wait = ctx.settings.get("machine_wait_minutes", 60) * 60
    try:
        with file_lock(ctx.lock_path(f"machine-{name}"), timeout=wait, inherit=True):
            code, message = machine_preflight(machine)
            if code:
                return code, message
            url = f"{machine['ssh']}:{machine['checkout']}"
            env = dict(os.environ)
            env.setdefault("GIT_SSH_COMMAND", "ssh " + " ".join(SSH_OPTS))
            push = run(["git", "push", "--force", "--quiet", url, f"{sha}:{REMOTE_REF}"],
                       cwd=worktree, env=env)
            if push.returncode:
                return 3, f"could not send branch to {machine['ssh']}: {push.stderr.strip()}"
            login_shell = '"${SHELL:-/bin/sh}" -lc ' + shlex.quote(check["run"])
            # The pid file protects a run the local lock can't: one whose ssh dropped
            # while the remote side kept going. Nothing is reset while it's alive.
            script = (
                f"cd {remote_path(machine['checkout'])} || exit {EXIT_NOT_SET_UP}; "
                f"test -f {MARKER} || exit {EXIT_NOT_SET_UP}; "
                f'if [ -f {RUN_PID_FILE} ] && kill -0 "$(cat {RUN_PID_FILE})" 2>/dev/null; '
                f"then exit {EXIT_RUN_ACTIVE}; fi; "
                f"echo $$ > {RUN_PID_FILE}; trap 'rm -f {RUN_PID_FILE}' EXIT; "
                f"git checkout --quiet --force --detach {sha} && git clean -fdq && {login_shell}"
            )
            with open(log, "w") as out:
                code = run_group(bounded(check, ssh_cmd(machine["ssh"], script, tty=True)),
                                 stdout=out).returncode
            if code == 255:
                return 3, f"lost connection to {machine['ssh']}"
            if code == EXIT_RUN_ACTIVE:
                return 3, f"an earlier run is still going on {machine['ssh']}"
            if code == EXIT_NOT_SET_UP:
                return 2, f"{machine['checkout']} on {machine['ssh']} is not set up"
            return (0, "") if code == 0 else (1, f"check exited {code}")
    except TimeoutError:
        return 3, f"machine {name} stayed busy for {wait // 60} minutes"


def fingerprint(ctx, check):
    """A passed check only counts for the exact command and machine that ran it."""
    machine = ctx.settings.get("machines", {}).get(check.get("machine"), {})
    return json.dumps([check["run"], check.get("machine"), machine], sort_keys=True)


def changed_by_check(worktree, sha):
    if git("rev-parse", "HEAD", cwd=worktree) != sha:
        return "a check moved HEAD"
    dirty = git("status", "--porcelain", cwd=worktree)
    if dirty:
        return "a check changed files in the worktree: " + ", ".join(dirty.splitlines()[:5])
    return None


def run_checks(ctx, slug, item, agent=None):
    """Return (exit_code, report). Fails closed on files no check or exemption covers."""
    worktree = item["worktree"]
    sha = git("rev-parse", "HEAD", cwd=worktree)
    changed = [p for p in changed_files(ctx, item["branch"]) if not p.startswith(f"{BACKLOG_DIR}/")]
    checks = ctx.settings.get("checks", [])
    exempt = ctx.settings.get("no_checks_needed", [])
    selected = [c for c in checks if any(path_matches(p, w) for p in changed for w in c["when"])]
    uncovered = [
        p for p in changed
        if not any(path_matches(p, w) for c in checks for w in c["when"])
        and not any(path_matches(p, e) for e in exempt)
    ]
    report = {"sha": sha, "changed": changed, "ran": [], "skipped": []}
    if uncovered:
        report.update(ok=False, uncovered=uncovered,
                      message="no check or exemption covers these files; escalate")
        return 1, report
    for check in selected:
        name = check["name"]
        passed = item["checks"].get(name)
        if (passed and passed["fingerprint"] == fingerprint(ctx, check)
                and git_ok("diff", "--quiet", passed["sha"], sha, "--", ".",
                           f":(exclude){BACKLOG_DIR}", cwd=worktree)):
            report["skipped"].append(name)
            continue
        log = ctx.state_dir / "logs" / f"{slug}-{name}.log"
        started = time.time()
        if check.get("machine"):
            code, message = run_machine_check(ctx, check, worktree, sha, log)
        else:
            code = run_local_check(check, worktree, log)
            code, message = (0, "") if code == 0 else (1, f"check exited {code}")
        log_event(ctx, "check", item=slug, agent=agent, check=name, exit=code,
               seconds=round(time.time() - started))
        report["ran"].append(name)
        if code:
            report.update(ok=False, failed=name, message=message, log=str(log))
            if code == 1:
                report["tail"] = tail(log)
            return code, report
        problem = changed_by_check(worktree, sha)
        if problem:
            report.update(ok=False, failed=name, log=str(log), message=problem)
            return 1, report
        result = {"sha": sha, "fingerprint": fingerprint(ctx, check)}
        item["checks"][name] = result
        with state(ctx) as st:
            if slug in st["items"]:
                st["items"][slug]["checks"][name] = result
    report["ok"] = True
    return 0, report


def require_holder(item, slug, agent):
    if item["status"] == "in_progress" and item["author"] == agent:
        return
    if item["status"] == "reviewing" and item["reviewer"] == agent:
        return
    raise Refused(f"{agent} does not hold {slug}")


def apply_check_failure(ctx, slug, code, reason):
    """Record what a machine failure means for the item; returns what happened to it."""
    if code not in (3, 4):
        return "unchanged"
    with state(ctx) as st:
        item = st["items"].get(slug)
        if not item:
            return "unchanged"
        if code == 3 and item["status"] == "reviewing":
            item.update(status="needs_review", reviewer=None, hold_until=time.time() + HOLD_SECONDS,
                        hold_reason=reason)
            log_event(ctx, "held", item=slug, reason=reason)
            return "held"
        if code == 4:
            item.update(status="needs_you", reason=reason, reviewer=None)
            log_event(ctx, "needs_you", item=slug, reason=reason)
            return "needs_you"
    return "unchanged"


def wait_limit(ctx):
    return ctx.settings.get("machine_wait_minutes", 60) * 60


def item_lock(ctx, slug):
    """One check or merge per item, including one left running by a killed helper."""
    return file_lock(ctx.lock_path(f"item-{slug}"), timeout=wait_limit(ctx), inherit=True)


def cmd_check(ctx, args):
    ctx.require_agent(args.agent)
    with state(ctx) as st:
        item = require_item(st, args.slug)
        require_holder(item, args.slug, args.agent)
        item = json.loads(json.dumps(item))
    require_clean(item["worktree"])
    with item_lock(ctx, args.slug):
        code, report = run_checks(ctx, args.slug, item, args.agent)
    report["item_now"] = apply_check_failure(ctx, args.slug, code, report.get("message", ""))
    emit(report, code)


def update_main(ctx, sha):
    """Fast-forward main to exactly the tested commit. Returns (exit_code, message)."""
    ctx.refresh()
    tree = ctx.main_worktree()
    if not on_main_tip_or_behind(ctx, sha):
        return 5, "main moved during the merge; run merge again"
    if tree:
        # Re-read at the last moment: the user may have switched branches during the checks.
        if git("symbolic-ref", "HEAD", cwd=tree) != f"refs/heads/{ctx.main}":
            return 5, f"{tree} changed branch during the merge; run merge again"
        proc = run(["git", "merge", "--quiet", "--ff-only", "--no-overwrite-ignore", sha], cwd=tree)
        if proc.returncode:
            if not on_main_tip_or_behind(ctx, sha):
                return 5, "main moved during the merge; run merge again"
            return 6, f"could not update main in {tree}: {proc.stderr.strip() or proc.stdout.strip()}"
    else:
        old = git("rev-parse", f"refs/heads/{ctx.main}", cwd=ctx.root)
        if not git_ok("update-ref", f"refs/heads/{ctx.main}", sha, old, cwd=ctx.root):
            return 5, "main moved during the merge; run merge again"
    if git("rev-parse", f"refs/heads/{ctx.main}", cwd=ctx.root) != sha:
        return 6, f"main is not at the tested commit {sha} after the update"
    return 0, ""


def on_main_tip_or_behind(ctx, sha):
    """True if main can fast-forward to sha."""
    return git_ok("merge-base", "--is-ancestor", ctx.main, sha, cwd=ctx.root)


def rebase_in_progress(worktree):
    for name in ("rebase-merge", "rebase-apply"):
        if (Path(worktree) / git("rev-parse", "--git-path", name, cwd=worktree)).exists():
            return True
    return False


def merge_locked(ctx, args):
    with state(ctx) as st:
        reconcile(ctx, st)
        if args.slug not in st["items"]:
            for record in reversed(st["merged"]):
                if record["item"] == args.slug:
                    return 0, {"ok": True, "merged": record["sha"], "already_merged": True,
                               "warnings": record.get("warnings", [])}
        item = require_item(st, args.slug)
        if item["status"] != "reviewing" or item["reviewer"] != args.agent:
            raise Refused(f"{args.slug} is {item['status']}; only its reviewer can merge it")
        if item["author"] == args.agent:
            raise Refused("an agent cannot merge its own work")
        item = json.loads(json.dumps(item))
    worktree, branch = item["worktree"], item["branch"]
    item_path = f"{BACKLOG_DIR}/{args.slug}.md"

    if rebase_in_progress(worktree):
        return 5, {"ok": False, "message": "a rebase is in progress in the worktree: finish it "
                   "or run `git rebase --abort`, then merge again"}
    # A staged removal of the item is what an interrupted earlier attempt leaves behind.
    if git("status", "--porcelain", cwd=worktree) != f"D  {item_path}":
        require_clean(worktree)
    if git("rev-list", "--count", f"{ctx.main}..{branch}", cwd=ctx.root) == "0":
        raise Refused("branch has no commits ahead of main")

    # Measured before the item file's removal is folded into the last commit.
    stats = review_work(ctx, item, worktree)
    with state(ctx) as st:
        if args.slug in st["items"]:
            st["items"][args.slug]["review_work"] = stats

    if git_ok("cat-file", "-e", f"HEAD:{item_path}", cwd=worktree):
        git("rm", "--quiet", "--ignore-unmatch", item_path, cwd=worktree)
        git("commit", "--quiet", "--amend", "--no-edit", cwd=worktree)

    if not on_main_tip_or_behind(ctx, branch):
        rebase = run(["git", "rebase", "--quiet", ctx.main], cwd=worktree)
        if rebase.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=worktree)
            run(["git", "rebase", "--abort"], cwd=worktree)
            return 5, {"ok": False, "conflicts": conflicts.splitlines(),
                       "message": f"run `git rebase {ctx.main}` in the worktree, resolve, then merge again"}

    code, report = run_checks(ctx, args.slug, item, args.agent)
    if code:
        report["item_now"] = apply_check_failure(ctx, args.slug, code, report.get("message", ""))
        return code, report

    sha = report["sha"]
    if git("rev-parse", f"refs/heads/{branch}", cwd=ctx.root) != sha:
        report.update(ok=False, message="branch moved after the checks; run merge again")
        return 5, report
    # Recorded first, so a retry can tell that main was updated if we die right after.
    with state(ctx) as st:
        st["items"][args.slug]["merging"] = sha
    code, message = update_main(ctx, sha)
    if code:
        report.update(ok=False, message=message)
        with state(ctx) as st:
            current = st["items"][args.slug]
            current.pop("merging", None)
            if code == 6:
                current.update(status="needs_you", reason=message, reviewer=None)
                report["item_now"] = "needs_you"
                log_event(ctx, "needs_you", item=args.slug, reason=message)
        return code, report

    with state(ctx) as st:
        if args.slug in st["items"]:
            finish_merge(ctx, st, args.slug, sha)
        warnings = next((r.get("warnings", []) for r in reversed(st["merged"])
                         if r["item"] == args.slug), [])
    warnings = list(warnings)
    if ctx.settings.get("push_after_merge", False):
        proc = run(["git", "push", "--quiet", "origin", ctx.main], cwd=ctx.root)
        if proc.returncode:
            warnings.append(f"push failed: {proc.stderr.strip()}")
    report.update(ok=True, merged=sha, warnings=warnings)
    return 0, report


def cmd_merge(ctx, args):
    ctx.require_agent(args.agent)
    asked = time.time()
    with file_lock(ctx.lock_path("merge"), timeout=wait_limit(ctx), inherit=True):
        with item_lock(ctx, args.slug):
            started = time.time()
            code, report = merge_locked(ctx, args)
    log_event(ctx, "merge_attempt", item=args.slug, agent=args.agent, exit=code,
           lock_wait=round(started - asked), seconds=round(time.time() - started))
    emit(report, code)


def issue_progress(merged, on_main):
    """For each issue: which of its items landed, which remain, and whether it's finished."""
    issues = {}
    for record in merged:
        if record.get("issue"):
            issues.setdefault(record["issue"], {"merged": [], "remaining": []})["merged"].append(record["item"])
    for slug, item in on_main.items():
        if item.get("issue"):
            issues.setdefault(item["issue"], {"merged": [], "remaining": []})["remaining"].append(slug)
    for progress in issues.values():
        progress["complete"] = bool(progress["merged"]) and not progress["remaining"]
    return issues


def cmd_status(ctx, _args):
    with state(ctx) as st:
        reconcile(ctx, st)
        items = st["items"]
        on_main = read_backlog(ctx)
        attention = {}
        for slug, candidate in on_main.items():
            if slug not in items and candidate["worth"] == "yes":
                problem = item_problem(ctx, candidate, on_main)
                if problem:
                    attention[slug] = problem
        emit_obj = {
            "active": {s: {k: i.get(k) for k in ("status", "author", "reviewer", "branch")}
                       for s, i in items.items() if i["status"] in ACTIVE},
            "held": {s: i.get("hold_reason") for s, i in items.items()
                     if i.get("hold_until", 0) > time.time()},
            "needs_you": {s: i.get("reason") for s, i in items.items() if i["status"] == "needs_you"},
            "attention": attention,
            "open": sorted(s for s, c in on_main.items() if s not in items and c["worth"] == "yes"),
            "paused": st.get("paused"),
            "merged": st["merged"],
            "issues": issue_progress(st["merged"], on_main),
        }
    emit(emit_obj)


def cmd_pause(ctx, args):
    with state(ctx) as st:
        st["paused"] = {"at": time.time(), "now": args.now}
        paused = st["paused"]
    log_event(ctx, "pause", now=args.now)
    emit({"ok": True, "paused": paused})


def cmd_resume(ctx, _args):
    with state(ctx) as st:
        was = st.pop("paused", None)
    log_event(ctx, "resume")
    emit({"ok": True, "was_paused": bool(was)})


def parse_since(text):
    """'24h', '7d', an ISO date or time, or nothing for the whole run."""
    if not text:
        return 0.0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", text)
    if match:
        return time.time() - float(match[1]) * (3600 if match[2] == "h" else 86400)
    try:
        import datetime
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise Refused(f"--since takes 24h, 7d or an ISO date, not {text!r}", code=2)


def hours(seconds):
    return round(seconds / 3600, 2)


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def read_events(ctx, since):
    path = ctx.state_dir / "events.jsonl"
    events = []
    if path.exists():
        for line in path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if event.get("at", 0) >= since:
                    events.append(event)
    return events


def batches(merged, since):
    """For goal runs: each plan-batch merge closes a batch. Spans run plan to plan.

    `slowest` names the three items that took longest from claim to merge, which is
    where to look when a batch runs slow."""
    spans, start, count, items = [], None, 0, []
    for rec in merged:
        count += 1
        if rec.get("claimed_at"):
            items.append({"item": rec["item"], "hours": hours(rec["at"] - rec["claimed_at"])})
        if rec["item"].startswith("plan-batch-"):
            if start is not None and rec["at"] >= since:
                spans.append({"closed_by": rec["item"], "merged": count,
                              "hours": hours(rec["at"] - start),
                              "hours_per_item": hours((rec["at"] - start) / count),
                              "slowest": sorted(items, key=lambda i: -i["hours"])[:3]})
            start, count, items = rec["at"], 0, []
    return spans


def cmd_report(ctx, args):
    """Who did what, how long it took, and where time went. Read-only."""
    since = parse_since(args.since)
    path = ctx.state_dir / "state.json"
    st = json.loads(path.read_text()) if path.exists() else {"merged": [], "items": {}}
    merged = [r for r in st["merged"] if r.get("at", 0) >= since]
    events = read_events(ctx, since)
    agents = {name: {"built": 0, "plans_built": 0, "reviewed": 0, "reviews_with_fixes": 0,
                     "built_work": {"commits": 0, "files": 0, "lines": 0},
                     "review_fixes": {"commits": 0, "files": 0, "lines": 0},
                     "build_hours": [], "review_hours": [], "waited_hours": 0.0,
                     "check_hours": 0.0, "escalated": 0, "released": 0}
              for name in ctx.agents}
    timed = 0
    for rec in merged:
        author, reviewer = agents.get(rec["author"]), agents.get(rec.get("reviewer"))
        if author:
            author["plans_built" if rec["item"].startswith("plan-batch-") else "built"] += 1
            for key, value in (rec.get("author_work") or {}).items():
                author["built_work"][key] += value
            if rec.get("claimed_at") and rec.get("submitted_at"):
                author["build_hours"].append(rec["submitted_at"] - rec["claimed_at"])
        if reviewer:
            reviewer["reviewed"] += 1
            fixes = rec.get("review_work") or {}
            for key, value in fixes.items():
                reviewer["review_fixes"][key] += value
            reviewer["reviews_with_fixes"] += bool(fixes.get("commits"))
            if rec.get("review_started_at"):
                reviewer["review_hours"].append(rec["at"] - rec["review_started_at"])
        timed += bool(rec.get("claimed_at") and rec.get("review_started_at"))
    checks = {}
    for event in events:
        agent = agents.get(event.get("agent"))
        kind = event["event"]
        if kind == "wait" and agent:
            agent["waited_hours"] += event["seconds"] / 3600
        elif kind == "check":
            entry = checks.setdefault(event["check"], {"runs": 0, "failed": 0, "seconds": []})
            entry["runs"] += 1
            entry["failed"] += event["exit"] != 0
            entry["seconds"].append(event["seconds"])
            if agent:
                agent["check_hours"] += event["seconds"] / 3600
        elif kind in ("escalate", "release") and agent:
            agent["escalated" if kind == "escalate" else "released"] += 1
    for agent in agents.values():
        for key in ("build_hours", "review_hours"):
            value = median(agent.pop(key))
            agent["median_" + key] = None if value is None else hours(value)
        agent["waited_hours"] = round(agent["waited_hours"], 2)
        agent["check_hours"] = round(agent["check_hours"], 2)
    for entry in checks.values():
        seconds = entry.pop("seconds")
        entry["median_minutes"] = round(median(seconds) / 60, 1)
        entry["total_hours"] = hours(sum(seconds))
    lock_waits = [e["lock_wait"] for e in events if e["event"] == "merge_attempt"]
    emit({
        "since": since or None,
        "merged": len(merged),
        "timed": f"{timed} of {len(merged)} merged items have build and review times",
        "agents": agents,
        "checks": checks,
        "merge_lock_wait_hours": hours(sum(lock_waits)),
        "needs_you": {s: i.get("reason") for s, i in st["items"].items() if i["status"] == "needs_you"},
        "batches": batches(st["merged"], since),
    })


def cmd_setup_machine(ctx, args):
    machine = ctx.settings.get("machines", {}).get(args.name)
    if not machine:
        raise Refused(f"no machine named {args.name!r} in settings", code=2)
    path = remote_path(machine["checkout"])
    script = (
        f"if [ -e {path} ] && [ ! -f {path}/{MARKER} ]; then echo EXISTS; exit 3; fi; "
        f"mkdir -p {path} && cd {path} && git init --quiet && touch {MARKER}"
    )
    proc = ssh(machine["ssh"], script, timeout=30)
    if proc.returncode == 3:
        raise Refused(f"{machine['checkout']} exists and is not a pair checkout", code=2)
    if proc.returncode:
        raise Refused(f"setup failed: {proc.stderr.strip()}", code=3)
    emit({"ok": True, "machine": args.name, "checkout": machine["checkout"]})


def cmd_doctor(ctx, _args):
    problems, warnings = [], []
    try:
        with tempfile.TemporaryFile(dir=ctx.common):
            pass
    except OSError as err:
        problems.append(f"cannot write to {ctx.common} ({err.strerror}). Commits and run state "
                        "both need this; a sandbox protecting .git causes this.")
    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{ctx.main}", cwd=ctx.root):
        problems.append(f"main branch {ctx.main!r} does not exist")
    if os.environ.get("HERDR_ENV") != "1":
        problems.append("not running inside a herdr pane")
    if not shutil.which("herdr"):
        problems.append("herdr is not on PATH")
    for name, agent in ctx.agents.items():
        if not shutil.which(agent.get("kind", "")):
            problems.append(f"agent {name}: {agent.get('kind')!r} is not on PATH")
    if not ctx.settings.get("checks"):
        problems.append("settings define no checks")
    for name, machine in ctx.settings.get("machines", {}).items():
        code, message = machine_preflight(machine)
        if code:
            (problems if code == 2 else warnings).append(f"machine {name}: {message}")
    for check in ctx.settings.get("checks", []):
        if check.get("machine") and check["machine"] not in ctx.settings.get("machines", {}):
            problems.append(f"check {check.get('name')}: undefined machine {check['machine']!r}")
    if problems:
        emit({"ok": False, "problems": problems, "warnings": warnings}, 2)
    on_main = read_backlog(ctx)
    if not on_main:
        warnings.append(f"no items committed to {BACKLOG_DIR}/ on {ctx.main}")
    for slug, item in on_main.items():
        if item["worth"] == "yes":
            problem = item_problem(ctx, item, on_main)
            if problem:
                warnings.append(f"{slug}: {problem}")
    emit({"ok": True, "problems": [], "warnings": warnings, "agents": ctx.agents})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("doctor", cmd_doctor), ("status", cmd_status), ("resume", cmd_resume)):
        sub.add_parser(name).set_defaults(func=func)
    pause = sub.add_parser("pause", help="hold plan-batch items; --now holds every unclaimed item")
    pause.add_argument("--now", action="store_true")
    pause.set_defaults(func=cmd_pause)
    next_cmd = sub.add_parser("next")
    next_cmd.add_argument("--agent", required=True)
    next_cmd.add_argument("--wait-seconds", type=int, default=0)
    next_cmd.set_defaults(func=cmd_next)
    for name, func in (("submit", cmd_submit), ("check", cmd_check), ("merge", cmd_merge)):
        command = sub.add_parser(name)
        command.add_argument("slug")
        command.add_argument("--agent", required=True)
        command.set_defaults(func=func)
    release = sub.add_parser("release")
    release.add_argument("slug")
    release.add_argument("--agent")
    release.add_argument("--force", action="store_true")
    release.add_argument("--reason", required=True)
    release.set_defaults(func=cmd_release)
    escalate = sub.add_parser("escalate")
    escalate.add_argument("slug")
    escalate.add_argument("--agent", required=True)
    escalate.add_argument("--reason", required=True)
    escalate.set_defaults(func=cmd_escalate)
    requeue = sub.add_parser("requeue")
    requeue.add_argument("slug")
    requeue.add_argument("--to", choices=["open", "needs_review"], required=True)
    requeue.set_defaults(func=cmd_requeue)
    report = sub.add_parser("report")
    report.add_argument("--since", help="24h, 7d or an ISO date; default: the whole run")
    report.set_defaults(func=cmd_report)
    setup = sub.add_parser("setup-machine")
    setup.add_argument("name")
    setup.set_defaults(func=cmd_setup_machine)
    args = parser.parse_args()
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop_children)
    try:
        args.func(Ctx(), args)
    except Refused as err:
        emit({"ok": False, "message": str(err), **err.extra}, err.code)
    except TimeoutError as err:
        emit({"ok": False, "message": f"still locked by an earlier run: {err}"}, 7)
    except OSError as err:
        emit({"ok": False, "message": f"cannot write: {err}. A sandbox protecting .git causes "
              "this. Tell the user, who decides what access to grant."}, 2)


if __name__ == "__main__":
    main()
