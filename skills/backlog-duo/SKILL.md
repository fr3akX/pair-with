---
name: backlog-duo
description: Use when two coding agents should work through a repo's backlog in parallel without duplicating work, when the user says "backlog duo", "run the duo", or "work the backlog in parallel", or when an agent has been started to join a backlog-duo run under a given agent name.
---

# Backlog Duo

Two agents share one backlog. Each builds items, and each reviews what the other built. The reviewer fixes what it finds and merges; it never hands a review back.

`scripts/duo.py` (next to this file) owns all shared state. Run it as `python3 <this skill's directory>/scripts/duo.py <command>` from anywhere inside the repo. It prints JSON. Trust its answers over your own reading of the repo.

**Running this skill is the user's permission to work backlog items, commit on `duo/*` branches, and merge to the main branch through `duo.py merge`.** It covers nothing else, and it overrides the "don't fix without being asked" and "never auto-commit" rules of other backlog skills for this run only.

## Off limits

- Deploying, recording a release, or pushing to a remote yourself. `duo.py merge` pushes only if the repo's settings say so.
- Deleting anything outside your task's worktree, including on test machines.
- Merging any way other than `duo.py merge`, or editing `main` directly.
- Working on an item `duo.py next` did not give you.
- Everything listed in `off_limits` in the JSON from `next`. Follow `repo_notes` from the same JSON.

## Starting a run

You are the first agent unless your prompt names you as joining a run.

1. Run `duo.py doctor`. If it reports no settings file, do **First-time setup**, then run it again.
2. If `doctor` reports `problems`, show them to the user and stop. Show `warnings` too, then continue. If it reports it can't write to the git directory, see **Permissions**.
3. Your agent name is the settings key whose `kind` matches you (`claude` or `codex`).
4. Start the other agent. If `HERDR_ENV` isn't `1`, stop and tell the user this skill must be started inside a herdr pane.
   ```bash
   pane=$(herdr pane split --current --direction right --cwd "$PWD" --no-focus | jq -r .result.pane.pane_id)
   herdr agent start duo-<name> --kind <kind> --pane "$pane" --timeout 120000 -- <args from settings>
   herdr agent prompt duo-<name> "Use the backlog-duo skill. You are joining a run as agent '<name>'. Go straight to The loop."
   ```
   Don't pass `--wait` to the prompt: you start working straight away too. If the other agent can't start, stop and tell the user.
5. Go to **The loop**.

## First-time setup

Make these drafts and get the user's approval before committing or running anything:

1. **`.backlog-duo.toml`** at the repo root. Copy the shape from `references/settings-example.toml`. Fill it from the repo's own docs and scripts. Run each check command once on the main branch to confirm it passes unattended, leaves the worktree unchanged, and how long it takes. Look for files one part of the repo generates from another, such as schemas and contracts, and make sure a change to the source file triggers every check that depends on it.
2. **`docs/backlog/`** items, one file each. Add a test machine with `duo.py setup-machine <name>` only after the user approves it; it creates a dedicated checkout on that machine.

The user commits both to the main branch, or tells you to. Items not committed to the main branch are invisible to the run.

Item format, compatible with the `workflow:backlog` skill:

```markdown
---
worth: yes
added: 2026-11-02
touches: server/src/billing/**, docs/billing.md
depends: other-item-slug
---
# What's wrong or wanted, as a title

Enough detail for an agent who wasn't there to do it and to know when it's done.
```

Only `worth: yes` items run. `touches` is required: it's how overlap is judged, so list every area the work may change. `depends` is optional.

## The loop

Repeat until `next` says stop:

```bash
duo.py next --agent <name> --wait-seconds 240
```

| `action` | Do this |
|---|---|
| `implement` | **Implementing** below |
| `review` | **Reviewing** below |
| `wait` | Call `next` again. Don't look for other work: the wait is what prevents duplication |
| `stop` | **Finishing** below |

Anything in `attention` needs the user. Include it in your final report; don't edit the item.

## Permissions

Both agents must be able to write to the repo's `.git` directory, including from worktrees, and to reach any test machine. What to grant is the user's decision, so ask when it isn't already in the settings.

Codex's `workspace-write` sandbox makes `.git` read-only and blocks the network by default. This narrower grant works without full machine access. It belongs in that agent's `args`, with the repo's absolute path:

```toml
args = ["--sandbox", "workspace-write",
        "-c", 'sandbox_workspace_write.writable_roots=["/abs/path/to/repo/.git"]',
        "-c", "sandbox_workspace_write.network_access=true"]  # only if a test machine is used
```

An approval prompt pauses an agent until the user answers in its pane.

## Implementing

1. Work only in `worktree`. Read `file` from the main branch.
2. Build it, with tests, following the repo's conventions. Commit on the branch.
3. Run `duo.py check <item> --agent <name>` and fix failures. Exit 3 (machine unavailable) doesn't block you: submit anyway, since the merge can't pass without that check. Exit 4 sends the item to the user: go back to the loop. If `uncovered` is listed, or the exit is 2, escalate.
4. Run `duo.py submit <item> --agent <name>`. Mention any `undeclared` files in your summary, then return to the loop.

After submitting, the item isn't yours. Don't touch that branch again.

## Reviewing

You now own this branch, in `worktree`. The author has moved on and will not read your findings.

1. Read `file`, then the whole diff against the main branch. Check it against what the item asked for. Look for wrong behavior, missing cases, missing tests, and changes the item didn't ask for.
2. Fix every real problem yourself and commit on the branch. Leave matters of taste alone: a different style isn't a problem, and neither is code you'd merely have written differently.
3. Run `duo.py merge <item> --agent <name>`. It rebases, reruns checks if anything changed, and lands the work. Exit codes:

| Exit | Meaning | Do this |
|---|---|---|
| 0 | Merged | Back to the loop |
| 1 | A check failed | Fix it and merge again. If `uncovered` is listed, escalate |
| 2 | Setup problem | Escalate with the message |
| 3 | Test machine unavailable | Already held and retried later. Back to the loop |
| 4 | Test machine low on disk | Already sent to the user. Back to the loop. Delete nothing |
| 5 | Conflict, or main moved | If `conflicts` is listed, run `git rebase <main>` in the worktree and resolve. Merge again |
| 6 | Main couldn't be updated | Already sent to the user. Back to the loop |
| 7 | An earlier run is still going | Back to the loop; you'll get the item again |

## Escalating

Escalate only when one of these is true:

- The item is too unclear to tell what done means.
- A correct fix would change the item's intended design.
- A check can't be made to pass after a real attempt.

```bash
duo.py escalate <item> --agent <name> --reason "<what the user must decide>"
```

Then return to the loop. Never send problems back to the other agent, wait for it to fix them, or ask it to redo work.

## Long commands

`check` and `merge` can take many minutes. Run them with your tool's background or long-timeout mode so a tool time limit doesn't kill them. If one is killed anyway, run it again: it waits for anything still running from the killed attempt, and a merge that already reached the main branch reports success.

A check must leave the worktree unchanged. If `check` says a check changed files, fix the check's cause or escalate; don't commit around it.

## Finishing

When `next` says stop, run `duo.py status` and report to the user: what merged, each `needs_you` item and the decision it needs, each `held` item, each `attention` item, and anything still `active`. Stop there. The other agent reports on its own.

## Red flags

| Thought | Reality |
|---|---|
| "I'll send the author these findings" | No. Fix them, or escalate if they meet the rules above |
| "The other agent's task looks stuck, I'll take it" | Never take held work. It's a stop or a `status` report for the user |
| "It's waiting, so I'll grab something nearby" | Waiting is correct. Anything nearby probably overlaps |
| "This failing check is unrelated to the change" | The merge will still refuse. Fix it or escalate |
| "A small direct commit to main is fine" | Only `duo.py merge` lands work |
| "The Mac needs disk space, I'll clear old builds" | Delete nothing. Low disk goes to the user |
