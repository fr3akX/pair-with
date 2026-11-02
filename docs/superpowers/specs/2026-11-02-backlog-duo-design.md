# Backlog Duo design

## Why

Maris runs parallel agents by hand: starting each Codex run, choosing tasks by intuition so they don't overlap, and passing finished work along for review. The goal is to remove that dispatching, so two agents can work one backlog at once and improve each other's work.

## Goals

- Two agents (Claude Opus and Codex with `gpt-6.1-sol` or `gpt-6-astra`) work one backlog in parallel with no duplicated work.
- When one finishes a task it moves on while the other reviews that work.
- The reviewer fixes what it finds itself instead of handing a review back.
- One universal skill that works in any repo. Anything repo-specific lives in a small settings file. `weat` is the first repo.

## Non-goals

- More than two agents.
- Running without herdr. It can be added later if there's a need.
- Deploying or recording releases. Those stay with the user.
- Running agents on remote machines. They stay on the Linux machine, and remote machines only run tests.

## Anti-goals

These would count as failure even if everything technically worked:

- Both agents building the same thing.
- An agent reviewing its own work.
- Untested code reaching `main`, particularly iOS work merged because the Mac was unavailable.
- An agent deleting evidence or build folders to free disk space.
- Damage to uncommitted work in the user's main checkout.

## Decided by the user

- The two-agent shape, the moving-on behavior, and fix-instead-of-hand-back.
- The skill is universal, with settings per repo.
- Overlap is judged from what each item declares it touches.
- Each task gets its own branch and worktree. Reviews come before new work. Agents never self-review.
- The reviewer's fixes aren't reviewed again. Merging is the final step, and tests are the only gate on those fixes.
- A task that needs unmerged work waits for that merge.
- The user is involved only when an item is unclear, a fix would change its design, or tests can't be made to pass.
- Agents can merge to `main`, but never deploy or record a release.
- Agents run on Linux. iOS is tested on the Mac over SSH, in one dedicated checkout, one run at a time. If the Mac is unavailable, that work waits. On low disk, the run asks the user and deletes nothing.
- One command starts a run: that session is the first agent, and it starts the other in a herdr pane. Without herdr it stops and says so.
- A first run in a new repo drafts settings and backlog folder and waits for approval.
- Both agents read one skill file, linked into `~/.claude/skills` and `~/.codex/skills` from this repo.

## Calls made while building (please check)

These go beyond what was discussed:

1. **Live status is not stored in item files.** The playback said items would carry a status. Instead, item files on `main` hold only the description, and claims and status live in `<git common dir>/backlog-duo/state.json` under a file lock. A status in a tracked file would need a commit to `main` on every claim, and each branch would see a different status.
2. **A helper script owns the rules that can be enforced.** `scripts/duo.py` handles claiming, overlap, no self-review, checks and merging, so these don't rely on agents following prose. It uses only the Python standard library and needs Python 3.11 or later on the Linux machine.
3. **Overlap is conservative.** Declared areas are compared by their fixed path prefix, and a false positive only makes two items run one after the other. A pattern starting with a wildcard overlaps everything. Files a branch actually changed count as well, once it's submitted.
4. **Items with no `touches` list aren't claimed.** They're reported to the user.
5. **A cap on unreviewed work** (`max_unreviewed`, default 2). An agent waits once it has that many items awaiting review, so a fast agent can't bury a slow one.
6. **Waiting is bounded** (`max_wait_minutes`, default 120). Agents never take over each other's work: if one dies, the other stops and reports it.
7. **Unmatched files block a merge.** A changed file no check or exemption covers fails closed, and the reviewer escalates it. This is the main protection, given there's no CI.
8. **Merges are linear.** The branch rebases onto `main`, and `main` is fast-forwarded, which matches the repo's mostly linear history. The item file is removed in the last commit, following the backlog skill's lifecycle.
9. **`main` is updated in the checkout that has it checked out**, with `git merge --ff-only`. Uncommitted work elsewhere survives. An overlap with uncommitted work refuses to merge and goes to the user.
10. **No pushing by default** (`push_after_merge = false`), since a push is outward-facing. iOS branches go straight to the Mac, not through GitHub.
11. **The Mac checkout is marked.** `setup-machine` creates it only if the folder doesn't exist, and each run requires the marker file, because each run force-resets and cleans that checkout. A person's checkout can't be used by mistake. Ignored files, including the build folder, survive between runs.
12. **Worktrees live in `<repo>/.backlog-duo-worktrees/`**, excluded through `.git/info/exclude`, so no tracked file changes and they stay inside the agents' working area.
13. **`off_limits` is advisory.** It's text shown to agents, not a technical block. `duo.py` can't stop an agent from running `kubectl`.
14. **A check must leave its worktree unchanged.** Otherwise what was tested isn't what merges. `main` is moved to the exact tested commit, and a rename counts as both of its paths.
15. **Interrupted commands recover from git.** Children keep the helper's locks alive, so a retry waits for a killed run's check to end, with a time limit enforced outside the helper. The tested commit is recorded before `main` moves, so a retry or the other agent's next call can finish an interrupted merge. Items whose file or branch disappears go to the user.
16. **The Mac refuses to reset while a run is alive**, using a PID file in the checkout, for runs whose SSH connection dropped.

## Known limits

- **Agent permissions are the user's decision.** Approval prompts pause an agent until the user answers in its pane. Codex's `workspace-write` sandbox makes `.git` read-only (confirmed), and a `writable_roots` grant on the repo's `.git` allows commits while other writes stay blocked (confirmed). `SKILL.md` documents it. That grant still lets Codex rewrite anything in `.git`, including hooks.
- **The real Mac and herdr startup are untested.** The tests cover `duo.py` with real git repos and a stand-in for SSH.
- **The Mac has 5.9 GB free today.** Below the configured minimum, iOS work goes to the user until space is cleared.
- **The web check's prerequisites are unconfirmed.** First-time setup must run each check to confirm it works unattended.
- **Each worktree builds from scratch**, unless a check sets its own shared build folder (`CARGO_TARGET_DIR`, for example).

## Left to the builder

- The exact wording of agent prompts.
- Settings defaults not listed here.
- Whether to add a `CLAUDE.md` or `AGENTS.md` to a repo. The skill doesn't require either.
