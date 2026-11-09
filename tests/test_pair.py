"""End-to-end tests for pair.py against throwaway git repos. Run: python3 -m unittest -v"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "skills/pair-with/scripts/pair.py"

FAKE_SSH = """#!/bin/sh
# Stand-in for ssh: drops options and the host, runs the command locally.
[ -n "$FAKE_SSH_DOWN" ] && exit 255
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
exec sh -c "$*"
"""

BASE_SETTINGS = """
main_branch = "main"
max_unreviewed = {cap}
no_checks_needed = ["docs/**"]

[agents.claude]
kind = "claude"
[agents.codex]
kind = "codex"

[[checks]]
name = "server"
when = ["server/**", "web/**"]
run = "{run}"
"""


class Repo:
    def __init__(self, tmp, cap=2, run="test ! -e server/FAIL", extra=""):
        self.root = Path(tmp) / "repo"
        self.root.mkdir()
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.git("init", "-q", "-b", "main")
        (self.root / ".backlog-duo.toml").write_text(BASE_SETTINGS.format(cap=cap, run=run) + extra)
        for folder in ("server", "web", "other"):
            (self.root / folder).mkdir()
            (self.root / folder / "a.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def item(self, slug, touches="server/**", worth="yes", added="2026-01-01", depends=None,
             issue=None, priority=None):
        folder = self.root / "docs/backlog"
        folder.mkdir(parents=True, exist_ok=True)
        lines = ["---", f"worth: {worth}", f"added: {added}"]
        if touches:
            lines.append(f"touches: {touches}")
        if depends:
            lines.append(f"depends: {depends}")
        if issue:
            lines.append(f"issue: {issue}")
        if priority:
            lines.append(f"priority: {priority}")
        (folder / f"{slug}.md").write_text("\n".join(lines + ["---", f"# {slug}", ""]))
        self.git("add", ".")
        self.git("commit", "-qm", f"add {slug}")

    def pair(self, *args):
        proc = subprocess.run(["python3", str(HELPER), *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout)

    def next(self, agent):
        return self.pair("next", "--agent", agent)[1]

    def work(self, answer, path="server/a.txt", text="changed\n"):
        tree = Path(answer["worktree"])
        (tree / path).write_text(text)
        self.git("add", ".", cwd=tree)
        self.git("commit", "-qm", f"work on {answer['item']}", cwd=tree)

    def do_item(self, agent, path="server/a.txt"):
        answer = self.next(agent)
        self.work(answer, path)
        code, out = self.pair("submit", answer["item"], "--agent", agent)
        assert code == 0, out
        return answer


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def repo(self, **kwargs):
        return Repo(self.tmp.name, **kwargs)


class Claiming(Base):
    def test_simultaneous_agents_never_claim_the_same_item(self):
        repo = self.repo()
        repo.item("one")
        repo.item("two", touches="web/**")
        with ThreadPoolExecutor(2) as pool:
            answers = list(pool.map(repo.next, ["claude", "codex"]))
        self.assertEqual([a["action"] for a in answers], ["implement", "implement"])
        self.assertNotEqual(answers[0]["item"], answers[1]["item"])

    def test_single_item_under_many_racing_calls_goes_to_one_agent(self):
        repo = self.repo()
        repo.item("one")
        with ThreadPoolExecutor(8) as pool:
            answers = list(pool.map(repo.next, ["claude", "codex"] * 4))
        holders = {a.get("author") for a in answers if a["action"] == "implement"}
        self.assertEqual(len(holders), 1)
        self.assertEqual({a["action"] for a in answers}, {"implement", "wait"})

    def test_overlapping_item_waits(self):
        repo = self.repo()
        repo.item("one", touches="server/**")
        repo.item("two", touches="server/a.txt")
        self.assertEqual(repo.next("claude")["item"], "one")
        answer = repo.next("codex")
        self.assertEqual(answer["action"], "wait")
        self.assertIn("two", answer["blocked"])

    def test_item_blocked_by_files_changed_beyond_its_touches(self):
        repo = self.repo()
        repo.item("one", touches="server/**", added="2026-01-01")
        repo.item("two", touches="web/**", added="2026-01-02")
        answer = repo.next("claude")
        tree = Path(answer["worktree"])
        (tree / "web/a.txt").write_text("x\n")
        repo.git("add", ".", cwd=tree)
        repo.git("commit", "-qm", "strays", cwd=tree)
        code, out = repo.pair("submit", "one", "--agent", "claude")
        self.assertEqual(out["undeclared"], ["web/a.txt"])
        repo.pair("next", "--agent", "codex")  # codex now reviews one
        self.assertEqual(repo.next("claude")["action"], "wait")

    def test_item_without_touches_is_flagged_not_claimed(self):
        repo = self.repo()
        repo.item("vague", touches=None)
        answer = repo.next("claude")
        self.assertEqual(answer["action"], "stop")
        self.assertIn("vague", answer["attention"])

    def test_only_worth_yes_is_claimed(self):
        repo = self.repo()
        repo.item("later", worth="later")
        repo.item("never", worth="no")
        self.assertEqual(repo.next("claude")["action"], "stop")

    def test_dependency_waits_for_merge_and_unknown_dependency_is_flagged(self):
        repo = self.repo()
        repo.item("first", touches="server/**", added="2026-01-02")
        repo.item("second", touches="web/**", added="2026-01-01", depends="first")
        repo.item("typo", touches="other/**", depends="does-not-exist")
        answer = repo.next("claude")
        self.assertEqual(answer["item"], "first")
        other = repo.next("codex")
        self.assertEqual(other["action"], "wait")
        self.assertIn("second", other["blocked"])
        self.assertIn("typo", other["attention"])
        repo.work(answer)
        repo.pair("submit", "first", "--agent", "claude")
        self.assertEqual(repo.next("codex")["action"], "review")
        self.assertEqual(repo.pair("merge", "first", "--agent", "codex")[0], 0)
        self.assertEqual(repo.next("claude")["item"], "second")

    def test_agent_resumes_what_it_holds(self):
        repo = self.repo()
        repo.item("one")
        repo.item("two", touches="web/**")
        first = repo.next("claude")
        self.assertEqual(repo.next("claude")["item"], first["item"])

    def test_uncommitted_item_is_invisible(self):
        repo = self.repo()
        folder = repo.root / "docs/backlog"
        folder.mkdir(parents=True)
        (folder / "draft.md").write_text("---\nworth: yes\ntouches: web/**\n---\n# d\n")
        self.assertEqual(repo.next("claude")["action"], "stop")


class Priorities(Base):
    def test_higher_priority_is_claimed_before_older_items(self):
        repo = self.repo()
        repo.item("old", added="2026-01-01")
        repo.item("later-p1", touches="web/**", added="2026-01-02", priority="P1")
        repo.item("latest-p0", touches="other/**", added="2026-01-03", priority="P0")
        self.assertEqual(repo.next("claude")["item"], "latest-p0")
        self.assertEqual(repo.next("codex")["item"], "later-p1")

    def test_a_dependency_runs_at_the_priority_of_what_needs_it(self):
        repo = self.repo(cap=5)
        repo.item("p1-work", touches="web/**", added="2026-01-01", priority="P1")
        repo.item("base", added="2026-01-02")
        repo.item("p0-work", touches="other/**", added="2026-01-03", depends="base", priority="P0")
        self.assertEqual(repo.next("claude")["item"], "base")

    def test_an_unknown_priority_needs_the_user(self):
        repo = self.repo()
        repo.item("odd", priority="urgent")
        answer = repo.next("claude")
        self.assertEqual(answer["action"], "stop")
        self.assertIn("priority", answer["attention"]["odd"])


class Pausing(Base):
    def test_pause_holds_the_plan_item_until_resumed(self):
        repo = self.repo()
        repo.item("work")
        repo.item("plan-batch-2", touches="docs/backlog/**", added="2026-01-02")
        self.assertEqual(repo.pair("pause")[0], 0)
        self.assertEqual(repo.next("claude")["item"], "work")
        answer = repo.next("codex")
        self.assertEqual(answer["action"], "wait")
        self.assertIn("paused", answer["blocked"]["plan-batch-2"])
        self.assertTrue(repo.pair("status")[1]["paused"])
        repo.pair("resume")
        self.assertEqual(repo.next("codex")["item"], "plan-batch-2")

    def test_paused_run_with_nothing_active_stops_and_says_why(self):
        repo = self.repo()
        repo.item("plan-batch-2", touches="docs/backlog/**")
        repo.pair("pause")
        answer = repo.next("claude")
        self.assertEqual(answer["action"], "stop")
        self.assertIn("paused", answer["reason"])

    def test_pause_now_holds_every_unclaimed_item(self):
        repo = self.repo()
        repo.item("one")
        repo.item("two", touches="web/**")
        claimed = repo.next("claude")["item"]
        repo.pair("pause", "--now")
        answer = repo.next("codex")
        self.assertEqual(answer["action"], "wait")
        self.assertEqual(repo.next("claude")["item"], claimed)


class Review(Base):
    def test_review_comes_before_new_work(self):
        repo = self.repo()
        repo.item("one", added="2026-01-01")
        repo.item("two", touches="web/**", added="2026-01-02")
        repo.do_item("claude")
        answer = repo.next("codex")
        self.assertEqual((answer["action"], answer["item"]), ("review", "one"))
        self.assertEqual(answer["author"], "claude")

    def test_author_never_reviews_own_work_and_moves_on(self):
        repo = self.repo()
        repo.item("one", added="2026-01-01")
        repo.item("two", touches="web/**", added="2026-01-02")
        repo.do_item("claude")
        answer = repo.next("claude")
        self.assertEqual((answer["action"], answer["item"]), ("implement", "two"))

    def test_cap_on_unreviewed_work_stops_a_fast_agent(self):
        repo = self.repo(cap=1)
        repo.item("one", added="2026-01-01")
        repo.item("two", touches="web/**", added="2026-01-02")
        repo.do_item("claude")
        answer = repo.next("claude")
        self.assertEqual(answer["action"], "wait")
        self.assertIn("two", answer["blocked"])

    def test_wait_then_stop_when_everything_is_done(self):
        repo = self.repo()
        repo.item("one")
        repo.do_item("claude")
        self.assertEqual(repo.next("claude")["action"], "wait")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        self.assertEqual(repo.next("claude")["action"], "stop")
        self.assertEqual(repo.next("codex")["action"], "stop")

    def test_max_wait_stops_instead_of_waiting_forever(self):
        repo = self.repo()
        settings = repo.root / ".backlog-duo.toml"
        settings.write_text("max_wait_minutes = 0\n" + settings.read_text())
        repo.item("one")
        repo.next("claude")
        self.assertEqual(repo.next("codex")["action"], "wait")
        subprocess.run(["sleep", "0.05"])
        self.assertEqual(repo.next("codex")["action"], "stop")

    def test_released_review_returns_to_queue(self):
        repo = self.repo()
        repo.item("one")
        repo.do_item("claude")
        repo.next("codex")
        self.assertEqual(repo.pair("release", "one", "--agent", "codex", "--reason", "x")[0], 0)
        self.assertEqual(repo.next("codex")["action"], "review")

    def test_released_work_keeps_its_branch_when_reclaimed(self):
        repo = self.repo()
        repo.item("one")
        answer = repo.next("claude")
        repo.work(answer)
        repo.pair("release", "one", "--agent", "claude", "--reason", "x")
        again = repo.next("codex")
        self.assertEqual(again["worktree"], answer["worktree"])
        self.assertEqual((Path(again["worktree"]) / "server/a.txt").read_text(), "changed\n")

    def test_escalated_item_leaves_queue_and_keeps_blocking_overlaps(self):
        repo = self.repo()
        repo.item("one", added="2026-01-01")
        repo.item("two", added="2026-01-02")
        repo.do_item("claude")
        repo.next("codex")
        self.assertEqual(repo.pair("escalate", "one", "--agent", "codex", "--reason", "unclear")[0], 0)
        answer = repo.next("claude")
        self.assertEqual(answer["action"], "stop")
        self.assertIn("two", answer["blocked"])
        self.assertEqual(repo.pair("status")[1]["needs_you"], {"one": "unclear"})
        self.assertEqual(repo.pair("requeue", "one", "--to", "needs_review")[0], 0)
        self.assertEqual(repo.next("codex")["action"], "review")


class Merging(Base):
    def reviewed(self, repo, slug="one", path="server/a.txt"):
        repo.item(slug)
        repo.do_item("claude", path)
        return repo.next("codex")

    def test_merge_lands_on_main_and_removes_item_in_same_commit(self):
        repo = self.repo()
        answer = self.reviewed(repo)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual((repo.root / "server/a.txt").read_text(), "changed\n")
        self.assertFalse((repo.root / "docs/backlog/one.md").exists())
        files = repo.git("show", "--name-only", "--format=", "main").splitlines()
        self.assertEqual(set(files), {"server/a.txt", "docs/backlog/one.md"})
        self.assertFalse(Path(answer["worktree"]).exists())
        self.assertEqual(repo.git("branch", "--list", "duo/one"), "")

    def test_merge_keeps_unrelated_uncommitted_files_in_main_checkout(self):
        repo = self.repo()
        (repo.root / "other/a.txt").write_text("my edit\n")
        (repo.root / "notes.md").write_text("mine\n")
        self.reviewed(repo)
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        self.assertEqual((repo.root / "other/a.txt").read_text(), "my edit\n")
        self.assertEqual((repo.root / "notes.md").read_text(), "mine\n")

    def test_merge_stops_when_main_checkout_has_conflicting_edit(self):
        repo = self.repo()
        self.reviewed(repo)
        (repo.root / "server/a.txt").write_text("user's edit\n")
        before = repo.git("rev-parse", "main")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 6, out)
        self.assertEqual(repo.git("rev-parse", "main"), before)
        self.assertEqual((repo.root / "server/a.txt").read_text(), "user's edit\n")
        self.assertIn("one", repo.pair("status")[1]["needs_you"])

    def test_author_and_non_reviewers_cannot_merge(self):
        repo = self.repo()
        repo.item("one")
        repo.do_item("claude")
        self.assertEqual(repo.pair("merge", "one", "--agent", "claude")[0], 1)
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 1)  # not yet reviewing
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "one", "--agent", "claude")[0], 1)

    def test_failed_check_blocks_merge_then_fix_lands(self):
        repo = self.repo()
        answer = self.reviewed(repo)
        tree = Path(answer["worktree"])
        (tree / "server/FAIL").write_text("x")
        repo.git("add", ".", cwd=tree)
        repo.git("commit", "-qm", "bad", cwd=tree)
        before = repo.git("rev-parse", "main")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertEqual(out["failed"], "server")
        self.assertEqual(repo.git("rev-parse", "main"), before)
        repo.git("rm", "-q", "server/FAIL", cwd=tree)
        repo.git("commit", "-qm", "fix", cwd=tree)
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)

    def test_files_with_no_check_or_exemption_fail_closed(self):
        repo = self.repo()
        self.reviewed(repo, path="other/a.txt")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertEqual(out["uncovered"], ["other/a.txt"])

    def test_docs_only_change_needs_no_check(self):
        repo = self.repo()
        repo.item("one", touches="docs/x.md")
        answer = repo.next("claude")
        (Path(answer["worktree"]) / "docs/x.md").write_text("x\n")
        repo.git("add", ".", cwd=answer["worktree"])
        repo.git("commit", "-qm", "doc", cwd=answer["worktree"])
        repo.pair("submit", "one", "--agent", "claude")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)

    def test_moved_main_is_rebased_and_rechecked(self):
        repo = self.repo(run="date >> ../../../checks-ran; test ! -e FAIL")
        answer = self.reviewed(repo)
        self.assertEqual(repo.pair("check", "one", "--agent", "codex")[0], 0)
        ran = repo.root.parent / "checks-ran"
        (repo.root / "web/a.txt").write_text("from main\n")
        repo.git("add", "web/a.txt")
        repo.git("commit", "-qm", "main moved")
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        self.assertEqual(len(ran.read_text().splitlines()), 2)
        self.assertEqual((repo.root / "web/a.txt").read_text(), "from main\n")
        self.assertEqual((repo.root / "server/a.txt").read_text(), "changed\n")
        self.assertEqual(int(repo.git("rev-list", "--merges", "--count", "main")), 0)
        self.assertFalse(Path(answer["worktree"]).exists())

    def test_unchanged_code_is_not_rechecked(self):
        repo = self.repo(run="date >> ../../../checks-ran")
        self.reviewed(repo)
        self.assertEqual(repo.pair("check", "one", "--agent", "codex")[0], 0)
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        self.assertEqual(len((repo.root.parent / "checks-ran").read_text().splitlines()), 1)

    def test_rebase_conflict_reports_and_leaves_branch_for_reviewer(self):
        repo = self.repo()
        answer = self.reviewed(repo)
        (repo.root / "server/a.txt").write_text("conflict\n")
        repo.git("add", ".")
        repo.git("commit", "-qm", "conflict")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 5, out)
        self.assertEqual(out["conflicts"], ["server/a.txt"])
        self.assertEqual(repo.git("status", "--porcelain", cwd=answer["worktree"]), "")

    def test_merge_can_be_retried_after_failure(self):
        repo = self.repo()
        answer = self.reviewed(repo)
        (repo.root / "server/a.txt").write_text("user's edit\n")
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 6)
        repo.git("checkout", "--", "server/a.txt")
        repo.pair("requeue", "one", "--to", "needs_review")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        self.assertFalse(Path(answer["worktree"]).exists())

    def test_dirty_worktree_cannot_submit(self):
        repo = self.repo()
        repo.item("one")
        answer = repo.next("claude")
        repo.work(answer)
        (Path(answer["worktree"]) / "server/b.txt").write_text("loose\n")
        self.assertEqual(repo.pair("submit", "one", "--agent", "claude")[0], 1)

    def test_submit_with_no_commits_is_refused(self):
        repo = self.repo()
        repo.item("one")
        repo.next("claude")
        self.assertEqual(repo.pair("submit", "one", "--agent", "claude")[0], 1)


class CodexFindings(Base):
    """One regression per finding from the first Codex review."""

    def reviewed(self, repo, slug="one", path="server/a.txt", touches="server/**"):
        repo.item(slug, touches=touches)
        repo.do_item("claude", path)
        return repo.next("codex")

    def test_moving_checked_code_to_an_exempt_folder_still_runs_checks(self):
        repo = self.repo(run="date >> ../../../checks-ran")
        repo.item("one", touches="server/**, docs/**")
        answer = repo.next("claude")
        repo.git("mv", "server/a.txt", "docs/moved.txt", cwd=answer["worktree"])
        repo.git("commit", "-qm", "move", cwd=answer["worktree"])
        repo.pair("submit", "one", "--agent", "claude")
        repo.next("codex")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["ran"], ["server"])

    def test_merge_refuses_to_overwrite_ignored_local_file(self):
        repo = self.repo()
        (repo.root / ".gitignore").write_text(".env\n")
        repo.git("add", ".gitignore")
        repo.git("commit", "-qm", "ignore")
        (repo.root / ".env").write_text("my secrets\n")
        repo.item("one", touches="server/**")
        answer = repo.next("claude")
        tree = Path(answer["worktree"])
        (tree / ".env").write_text("theirs\n")
        (tree / "server/a.txt").write_text("changed\n")
        repo.git("add", "-f", ".env", "server/a.txt", cwd=tree)
        repo.git("commit", "-qm", "tracks env", cwd=tree)
        settings = repo.root / ".backlog-duo.toml"
        settings.write_text(settings.read_text().replace(
            'no_checks_needed = ["docs/**"]', 'no_checks_needed = ["docs/**", ".env"]'))
        repo.pair("submit", "one", "--agent", "claude")
        repo.next("codex")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 6, out)
        self.assertEqual((repo.root / ".env").read_text(), "my secrets\n")

    def test_check_that_changes_tracked_files_blocks_merge(self):
        repo = self.repo(run="echo generated >> server/a.txt")
        self.reviewed(repo)
        before = repo.git("rev-parse", "main")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn("changed files", out["message"])
        self.assertEqual(repo.git("rev-parse", "main"), before)

    def test_check_that_moves_head_blocks_merge(self):
        repo = self.repo(run="git commit -q --allow-empty -m sneaky")
        self.reviewed(repo)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn("moved HEAD", out["message"])

    def test_killed_helper_keeps_lock_until_its_check_ends(self):
        log = Path(self.tmp.name) / "overlap"
        run = (f"echo start >> {log}; sleep 2; echo end >> {log}")
        repo = self.repo(run=run)
        self.reviewed(repo)
        first = subprocess.Popen(["python3", str(HELPER), "check", "one", "--agent", "codex"],
                                 cwd=repo.root, env=repo.env, stdout=subprocess.DEVNULL)
        for _ in range(100):
            if log.exists():
                break
            time.sleep(0.05)
        first.kill()
        first.wait()
        code, out = repo.pair("check", "one", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(log.read_text().split(), ["start", "end", "start", "end"])

    def test_terminated_helper_stops_its_check(self):
        log = Path(self.tmp.name) / "stopped"
        repo = self.repo(run=f"echo start >> {log}; sleep 3; echo end >> {log}")
        self.reviewed(repo)
        first = subprocess.Popen(["python3", str(HELPER), "check", "one", "--agent", "codex"],
                                 cwd=repo.root, env=repo.env, stdout=subprocess.DEVNULL)
        for _ in range(100):
            if log.exists():
                break
            time.sleep(0.05)
        first.terminate()
        first.wait()
        time.sleep(3.5)
        self.assertEqual(log.read_text().split(), ["start"])

    def test_check_timeout_fails_the_check(self):
        repo = self.repo(run="sleep 30")
        settings = repo.root / ".backlog-duo.toml"
        settings.write_text(settings.read_text().replace(
            'name = "server"', 'name = "server"\ntimeout_minutes = 0.02'))
        repo.git("add", ".")
        repo.git("commit", "-qm", "short timeout")
        self.reviewed(repo)
        started = time.monotonic()
        code, out = repo.pair("check", "one", "--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertLess(time.monotonic() - started, 15)

    def interrupted_after_fast_forward(self, repo):
        """State as a merge killed right after updating main leaves it."""
        answer = self.reviewed(repo)
        tree = answer["worktree"]
        repo.git("rm", "-q", "docs/backlog/one.md", cwd=tree)
        repo.git("commit", "-q", "--amend", "--no-edit", cwd=tree)
        sha = repo.git("rev-parse", "HEAD", cwd=tree)
        repo.git("merge", "-q", "--ff-only", sha)
        path = Path(repo.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        path = path / "backlog-duo/state.json"
        state = json.loads(path.read_text())
        state["items"]["one"]["merging"] = sha
        path.write_text(json.dumps(state))
        return answer, sha

    def test_retrying_merge_after_interruption_finishes_it(self):
        repo = self.repo()
        answer, sha = self.interrupted_after_fast_forward(repo)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["merged"], sha)
        self.assertTrue(out["already_merged"])
        self.assertFalse(Path(answer["worktree"]).exists())

    def test_next_finishes_an_interrupted_merge(self):
        repo = self.repo()
        answer, sha = self.interrupted_after_fast_forward(repo)
        self.assertEqual(repo.next("claude")["action"], "stop")
        self.assertEqual(repo.pair("status")[1]["merged"][0]["sha"], sha)
        self.assertFalse(Path(answer["worktree"]).exists())

    def test_merge_retry_after_success_reports_success(self):
        repo = self.repo()
        self.reviewed(repo)
        code, first = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, first)
        code, second = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["merged"], first["merged"])

    def test_retry_with_staged_item_removal_works(self):
        repo = self.repo()
        answer = self.reviewed(repo)
        repo.git("rm", "-q", "docs/backlog/one.md", cwd=answer["worktree"])
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)

    def test_unfinished_rebase_is_reported_not_trampled(self):
        repo = self.repo()
        answer = self.reviewed(repo)
        (repo.root / "server/a.txt").write_text("conflict\n")
        repo.git("add", ".")
        repo.git("commit", "-qm", "conflict")
        subprocess.run(["git", "rebase", "main"], cwd=answer["worktree"], env=repo.env,
                       capture_output=True)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 5, out)
        self.assertIn("rebase is in progress", out["message"])

    def test_item_removed_from_main_goes_to_user(self):
        repo = self.repo()
        repo.item("one")
        answer = repo.next("claude")
        repo.git("rm", "-q", "docs/backlog/one.md")
        repo.git("commit", "-qm", "withdraw")
        self.assertEqual(repo.next("claude")["action"], "stop")
        self.assertIn("removed from main", repo.pair("status")[1]["needs_you"]["one"])
        self.assertTrue(Path(answer["worktree"]).exists())

    def test_deleted_branch_goes_to_user(self):
        repo = self.repo()
        repo.item("one")
        answer = repo.next("claude")
        repo.git("worktree", "remove", answer["worktree"])
        repo.git("branch", "-D", "duo/one")
        self.assertEqual(repo.next("claude")["action"], "stop")
        self.assertIn("no longer exists", repo.pair("status")[1]["needs_you"]["one"])

    def test_deleted_worktree_is_recreated_from_branch(self):
        repo = self.repo()
        repo.item("one")
        answer = repo.next("claude")
        repo.work(answer)
        shutil.rmtree(answer["worktree"])
        again = repo.next("claude")
        self.assertEqual(again["action"], "implement")
        self.assertEqual((Path(again["worktree"]) / "server/a.txt").read_text(), "changed\n")

    def test_switching_branch_during_checks_lands_on_main_only(self):
        repo = self.repo(run="git -C ../.. checkout -q -b elsewhere")
        self.reviewed(repo)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(repo.git("symbolic-ref", "--short", "HEAD"), "elsewhere")
        self.assertEqual(repo.git("rev-parse", "main"), out["merged"])
        self.assertNotEqual(repo.git("rev-parse", "elsewhere"), out["merged"])

    def test_duplicate_check_names_are_refused(self):
        repo = self.repo(extra='\n[[checks]]\nname = "server"\nwhen = ["web/**"]\nrun = "false"\n')
        repo.item("one")
        code, out = repo.pair("next", "--agent", "claude")
        self.assertEqual(code, 2, out)
        self.assertIn("unique", out["message"])

    def test_changed_check_command_invalidates_earlier_pass(self):
        repo = self.repo(run="true")
        self.reviewed(repo)
        self.assertEqual(repo.pair("check", "one", "--agent", "codex")[0], 0)
        settings = repo.root / ".backlog-duo.toml"
        settings.write_text(settings.read_text().replace('run = "true"', 'run = "false"'))
        code, out = repo.pair("check", "one", "--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertEqual(out["failed"], "server")

    def test_unwritable_git_dir_is_reported_as_json(self):
        repo = self.repo()
        repo.item("one")
        git_dir = repo.root / ".git"
        git_dir.chmod(0o555)
        self.addCleanup(git_dir.chmod, 0o755)
        code, out = repo.pair("doctor")
        self.assertEqual(code, 2, out)
        self.assertTrue(any("cannot write" in p for p in out["problems"]), out)
        code, out = repo.pair("next", "--agent", "claude")
        self.assertEqual(code, 2, out)
        self.assertIn("sandbox", out["message"])

    def test_doctor_changes_nothing(self):
        repo = self.repo()
        repo.item("one")
        repo.pair("doctor")
        self.assertFalse((repo.root / ".git/backlog-duo").exists())


class Intake(Base):
    def test_issue_is_complete_only_when_all_its_items_landed(self):
        repo = self.repo()
        repo.item("core", touches="server/**", issue="22", added="2026-01-01")
        repo.item("web", touches="web/**", issue="22", added="2026-01-02", depends="core")
        repo.item("unrelated", touches="other/**", worth="later")
        self.assertEqual(repo.pair("status")[1]["issues"],
                         {"22": {"merged": [], "remaining": ["core", "web"], "complete": False}})
        answer = repo.do_item("claude")
        self.assertEqual(answer["issue"], "22")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "core", "--agent", "codex")[0], 0)
        self.assertEqual(repo.pair("status")[1]["issues"],
                         {"22": {"merged": ["core"], "remaining": ["web"], "complete": False}})
        answer = repo.next("claude")
        self.assertEqual(answer["item"], "web")
        repo.work(answer, "web/a.txt")
        repo.pair("submit", "web", "--agent", "claude")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "web", "--agent", "codex")[0], 0)
        self.assertEqual(repo.pair("status")[1]["issues"],
                         {"22": {"merged": ["core", "web"], "remaining": [], "complete": True}})

    def test_escalated_item_keeps_its_issue_incomplete(self):
        repo = self.repo()
        repo.item("core", issue="22")
        repo.do_item("claude")
        repo.next("codex")
        repo.pair("escalate", "core", "--agent", "codex", "--reason", "unclear")
        self.assertFalse(repo.pair("status")[1]["issues"]["22"]["complete"])

    def test_brief_beside_items_is_not_treated_as_an_item(self):
        repo = self.repo()
        repo.item("core", issue="22")
        briefs = repo.root / "docs/backlog/briefs"
        briefs.mkdir()
        (briefs / "22-thing.md").write_text("# Brief\n")
        repo.git("add", ".")
        repo.git("commit", "-qm", "brief")
        self.assertEqual(repo.pair("status")[1]["open"], ["core"])
        answer = repo.do_item("claude")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", answer["item"], "--agent", "codex")[0], 0)
        self.assertTrue((repo.root / "docs/backlog/briefs/22-thing.md").exists())


class Machines(Base):
    def setUp(self):
        super().setUp()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        fake = self.bin / "ssh"
        fake.write_text(FAKE_SSH)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.checkout = Path(self.tmp.name) / "remote"

    def machine_repo(self, min_free=0):
        extra = textwrap.dedent(f"""
            [machines.mac]
            ssh = "fakehost"
            checkout = "{self.checkout}"
            min_free_gb = {min_free}

            [[checks]]
            name = "ios"
            when = ["other/**"]
            machine = "mac"
            run = "grep -q changed other/a.txt && pwd > ran-here"
        """)
        repo = self.repo(extra=extra)
        repo.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", SHELL="/bin/sh",
                        GIT_SSH_COMMAND=str(self.bin / "ssh"))
        return repo

    def reviewed(self, repo):
        repo.item("one", touches="other/**")
        repo.do_item("claude", "other/a.txt")
        repo.next("codex")

    def test_setup_refuses_to_take_over_existing_folder(self):
        repo = self.machine_repo()
        self.checkout.mkdir()
        self.assertEqual(repo.pair("setup-machine", "mac")[0], 2)

    def test_machine_check_runs_remotely_then_merges(self):
        repo = self.machine_repo()
        self.assertEqual(repo.pair("setup-machine", "mac")[0], 0)
        self.reviewed(repo)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["ran"], ["ios"])
        self.assertTrue((self.checkout / "ran-here").exists())

    def test_machine_not_set_up_is_refused(self):
        repo = self.machine_repo()
        self.reviewed(repo)
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 2, out)

    def test_unavailable_machine_holds_item_and_other_work_continues(self):
        repo = self.machine_repo()
        repo.pair("setup-machine", "mac")
        self.reviewed(repo)
        before = repo.git("rev-parse", "main")
        repo.item("two", touches="web/**")
        repo.env["FAKE_SSH_DOWN"] = "1"
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 3, out)
        self.assertEqual(out["item_now"], "held")
        self.assertEqual(repo.git("log", "-1", "--format=%s", "main"), "add two")
        self.assertNotEqual(before, "")
        answer = repo.next("codex")
        self.assertEqual((answer["action"], answer["item"]), ("implement", "two"))
        self.assertIn("one", repo.pair("status")[1]["held"])

    def test_live_run_on_machine_blocks_reset_of_its_checkout(self):
        repo = self.machine_repo()
        repo.pair("setup-machine", "mac")
        self.reviewed(repo)
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        (self.checkout / ".git/backlog-duo-run.pid").write_text(str(sleeper.pid))
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 3, out)
        self.assertIn("still going", out["message"])
        self.assertFalse((self.checkout / "other").exists())

    def test_stale_pid_file_does_not_block_the_machine(self):
        repo = self.machine_repo()
        repo.pair("setup-machine", "mac")
        self.reviewed(repo)
        sleeper = subprocess.Popen(["true"])
        sleeper.wait()
        (self.checkout / ".git/backlog-duo-run.pid").write_text(str(sleeper.pid))
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        self.assertFalse((self.checkout / ".git/backlog-duo-run.pid").exists())

    def test_low_disk_goes_to_user_and_never_merges(self):
        repo = self.machine_repo(min_free=10**9)
        repo.pair("setup-machine", "mac")
        self.reviewed(repo)
        before = repo.git("rev-parse", "main")
        code, out = repo.pair("merge", "one", "--agent", "codex")
        self.assertEqual(code, 4, out)
        self.assertEqual(out["item_now"], "needs_you")
        self.assertEqual(repo.git("rev-parse", "main"), before)
        self.assertIn("one", repo.pair("status")[1]["needs_you"])


class Reporting(Base):
    def fix(self, repo, answer, path="server/a.txt", text="fixed by reviewer\n"):
        tree = Path(answer["worktree"])
        (tree / path).write_text(text)
        repo.git("add", ".", cwd=tree)
        repo.git("commit", "-qm", "review fix", cwd=tree)

    def test_report_credits_the_builder_and_the_reviewer_separately(self):
        repo = self.repo()
        settings = repo.root / ".backlog-duo.toml"
        settings.write_text('report_exclude = ["web/**"]\n' + settings.read_text())
        repo.git("commit", "-qam", "exclude web from reports")
        repo.item("one", touches="server/**, web/**")
        answer = repo.next("claude")
        (Path(answer["worktree"]) / "web/a.txt").write_text("not counted\n")
        repo.work(answer, text="line one\nline two\n")
        self.assertEqual(repo.pair("submit", "one", "--agent", "claude")[0], 0)
        review = repo.next("codex")
        self.fix(repo, review)
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        code, out = repo.pair("report")
        self.assertEqual(code, 0, out)
        claude, codex = out["agents"]["claude"], out["agents"]["codex"]
        self.assertEqual((claude["built"], claude["reviewed"]), (1, 0))
        self.assertEqual((codex["built"], codex["reviewed"], codex["reviews_with_fixes"]), (0, 1, 1))
        self.assertEqual(claude["built_work"], {"commits": 1, "files": 1, "lines": 3})
        self.assertEqual(codex["review_fixes"], {"commits": 1, "files": 1, "lines": 3})
        self.assertIsNotNone(claude["median_build_hours"])
        self.assertIsNotNone(codex["median_review_hours"])
        self.assertEqual(out["checks"]["server"]["runs"], 1)
        self.assertEqual(out["timed"], "1 of 1 merged items have build and review times")

    def test_a_review_with_no_changes_counts_no_fixes(self):
        repo = self.repo()
        repo.item("one")
        repo.do_item("claude")
        repo.next("codex")
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        codex = repo.pair("report")[1]["agents"]["codex"]
        self.assertEqual((codex["reviewed"], codex["reviews_with_fixes"]), (1, 0))
        self.assertEqual(codex["review_fixes"]["commits"], 0)

    def test_reviewer_rebase_still_separates_the_work(self):
        repo = self.repo()
        repo.item("one")
        repo.item("two", touches="web/**")
        repo.do_item("claude")
        review = repo.next("codex")
        other = repo.next("claude")
        repo.work(other, "web/a.txt")
        repo.pair("submit", "two", "--agent", "claude")
        self.fix(repo, review)
        repo.git("rebase", "-q", "main", cwd=review["worktree"])
        self.assertEqual(repo.pair("merge", "one", "--agent", "codex")[0], 0)
        codex = repo.pair("report")[1]["agents"]["codex"]
        self.assertEqual(codex["review_fixes"]["commits"], 1)

    def test_waiting_and_escalations_are_recorded(self):
        repo = self.repo()
        repo.item("one")
        repo.next("claude")
        self.assertEqual(repo.next("codex")["action"], "wait")
        repo.pair("escalate", "one", "--agent", "claude", "--reason", "needs a real device")
        repo.next("codex")
        out = repo.pair("report")[1]
        self.assertEqual(out["agents"]["claude"]["escalated"], 1)
        self.assertIn("one", out["needs_you"])
        events = (repo.root / ".git/backlog-duo/events.jsonl").read_text().splitlines()
        self.assertIn("wait", [json.loads(e)["event"] for e in events])

    def test_goal_batches_run_from_plan_to_plan(self):
        repo = self.repo()
        for day, (slug, touches) in enumerate((("plan-batch-1", "docs/backlog/**"),
                                               ("a", "server/**"),
                                               ("plan-batch-2", "docs/backlog/**")), 1):
            repo.item(slug, touches=touches, added=f"2026-01-0{day}",
                      depends="a" if slug == "plan-batch-2" else None)
        for slug, path in (("plan-batch-1", "docs/note.md"), ("a", "server/a.txt"),
                           ("plan-batch-2", "docs/other.md")):
            answer = repo.next("claude")
            self.assertEqual(answer["item"], slug)
            repo.work(answer, path)
            repo.pair("submit", slug, "--agent", "claude")
            repo.next("codex")
            self.assertEqual(repo.pair("merge", slug, "--agent", "codex")[0], 0)
        out = repo.pair("report")[1]
        self.assertEqual(out["agents"]["claude"]["plans_built"], 2)
        self.assertEqual(out["agents"]["claude"]["built"], 1)
        self.assertEqual([b["closed_by"] for b in out["batches"]], ["plan-batch-2"])
        self.assertEqual(out["batches"][0]["merged"], 2)
        slowest = out["batches"][0]["slowest"]
        self.assertEqual({s["item"] for s in slowest}, {"a", "plan-batch-2"})
        self.assertTrue(all(s["hours"] >= 0 for s in slowest))

    def test_since_filters_and_report_changes_nothing(self):
        repo = self.repo()
        repo.item("one")
        repo.do_item("claude")
        repo.next("codex")
        repo.pair("merge", "one", "--agent", "codex")
        state = (repo.root / ".git/backlog-duo/state.json").read_text()
        self.assertEqual(repo.pair("report", "--since", "2999-01-01")[1]["merged"], 0)
        self.assertEqual(repo.pair("report", "--since", "1h")[1]["merged"], 1)
        self.assertEqual(repo.pair("report", "--since", "soon")[0], 2)
        self.assertEqual((repo.root / ".git/backlog-duo/state.json").read_text(), state)


if __name__ == "__main__":
    unittest.main()
