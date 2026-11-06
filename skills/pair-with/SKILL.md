---
name: pair-with
description: Use when the user says "pair with codex" or "pair with claude", with or without a model and effort such as "pair with codex astra high", or "pair on issue N" or "work the backlog in parallel", when two coding agents should work through a repo's backlog or a GitHub issue in parallel without duplicating work, or when an agent has been started to join a pair run under a given agent name.
---

# Pair

Two agents share one backlog. Each builds items, and each reviews what the other built. The reviewer fixes what it finds and merges; it never hands a review back. Vague work is settled with the user and split into items first, before the second agent starts.

`scripts/pair.py` (next to this file) owns all shared state. Run it as `python3 <this skill's directory>/scripts/pair.py <command>` from anywhere inside the repo. It prints JSON. Trust its answers over your own reading of the repo.

**Running this skill is the user's permission to work backlog items, commit on `duo/*` branches, and merge to the main branch through `pair.py merge`.** It covers nothing else, and it overrides the "don't fix without being asked" and "never auto-commit" rules of other backlog skills for this run only.

## Off limits

- Deploying, recording a release, or pushing to a remote yourself. `pair.py merge` pushes only if the repo's settings say so.
- Deleting anything outside your task's worktree, including on test machines.
- Merging any way other than `pair.py merge`, or editing `main` directly.
- Working on an item `pair.py next` did not give you.
- Everything listed in `off_limits` in the JSON from `next`. Follow `repo_notes` from the same JSON.

## Starting a run

You are the first agent unless your prompt names you as joining a run.

1. Run `pair.py doctor`. If it reports no settings file, do **First-time setup**, then run it again.
2. If `doctor` reports `problems`, show them to the user and stop. Show `warnings` too, then continue. If it reports it can't write to the git directory, see **Permissions**.
3. If the user named an issue or described work that has no items yet, do **Turning an issue into items**, then run `doctor` again. If they named an outcome with no fixed end, such as reaching parity, also do **Running toward a goal**. If they named no work and the backlog has no items, ask what to work on.
4. Your agent name is the settings key whose `kind` matches you (`claude` or `codex`). If the user asked to pair with your own kind, tell them a run needs one of each, and stop.
5. Start the other agent, using the args from **Choosing the other agent**. If `HERDR_ENV` isn't `1`, stop and tell the user this skill must be started inside a herdr pane.
   ```bash
   pane=$(herdr pane split --current --direction right --cwd "$PWD" --no-focus | jq -r .result.pane.pane_id)
   herdr agent start pair-<name> --kind <kind> --pane "$pane" --timeout 120000 -- <args>
   herdr agent prompt pair-<name> "Use the pair-with skill. You are joining a run as agent '<name>'. Go straight to The loop."
   ```
   Don't pass `--wait` to the prompt: you start working straight away too. If the other agent can't start, stop and tell the user.
6. Go to **The loop**.

## Choosing the other agent

The user can name a model and effort for the other agent: `pair with <kind> [model] [effort]`, such as `pair with codex astra high` or `pair with claude opus max`. Start from that agent's `args` in the settings, then apply what they named. Replace a flag that's already there; never add a second one.

| They said | Codex | Claude |
|---|---|---|
| `low`, `medium`, `high`, `xhigh` or `max` | `-c model_reasoning_effort="<level>"` | `--effort <level>` |
| `astra` | `-m gpt-6-astra` | |
| `sol` | `-m gpt-6.1-sol` | |
| any other word | `-m <word>` | `--model <word>` |

What they didn't name stays as the settings have it. This applies to this run only, so don't edit the settings.

## Turning an issue into items

Do this alone, with the user present, before the other agent starts. Once the run begins, agents can only ask each other, so every answer only the user can give must be settled here.

**1. Understand it.** Read the issue with its comments (`gh issue view <n> --comments`), the docs and code it touches, and anything it links to. Look for repo rules that shape the work, such as features that must exist in every client.

**2. Settle what's open.** List every point where a builder would have to guess and a wrong guess would matter. Answer what the repo can answer. Ask the user about the rest, most important first, one question per message, each with your recommended answer and the reason for it. Stop when nothing important is left. If the user says "use your judgment", take your recommendations and mark them as your calls in the brief.

**3. Write the brief** at `docs/backlog/briefs/<issue>-<topic>.md`: the goal, what's out of scope, each decision and who made it, and what done looks like. Name anything agents can't verify, such as a real-world test, as work left for the user.

**4. Split it into items** in `docs/backlog/`, using the format below, with `issue: <n>` on each. Every item must:

- leave the main branch working when it merges alone, with its own tests
- be small enough for one agent to build and another to review in one sitting
- stand alone: say what to build and how to tell it's done, and name the brief
- declare `touches` as narrowly as honesty allows, since overlapping items can't run at once

Put shared foundations first and use `depends` for anything needing them. Prefer splits that let two items run at once. A chain of dependencies leaves one agent waiting, so say so if parallel work isn't possible.

**5. Get approval.** Show the user the decisions and a table of items with what each covers, what it depends on, and which can run at the same time. Change what they ask for. Don't start without a yes.

**6. Commit.** Approval covers one commit on the main branch holding only the brief and item files. Stage those exact paths. If you're not on the main branch or anything else is already staged, stop and ask. Don't push.

Comment on, label, or close an issue only if the user asks.

## Running toward a goal

The run keeps itself going by making the last item of each batch plan the next batch. The approved commit holds the following:

- **A brief** at `docs/backlog/briefs/goal-<topic>.md`: the goal, how progress is measured, the test for done, and what only the user can do.
- **An item** `plan-batch-<n>`, with `touches: docs/backlog/**`, naming that brief and depending on every other item in the batch.

**Building a `plan-batch-<n>` item.** Measure progress from the repo's evidence, then do the first of these that applies:

1. If the done test is met, or everything left needs the user, add no new items. Record the result and what's left in the brief. This ends the run.
2. If the last two batches added no verified behaviour, do the same and say so.
3. Otherwise write the next 3 to 6 items, using the rules in **Turning an issue into items**, plus `plan-batch-<n+1>` depending on all of them.

**Reviewing a `plan-batch-<n>` item.** Check that the measurement is true, that each item moves the goal forward, and that nothing repeats work already done or waits on the user. Fix the plan yourself, then merge.

A goal never widens what's off limits. Deploying, pushing and anything else reserved for the user stay reserved.

## First-time setup

Make these drafts and get the user's approval before committing or running anything:

1. **`.backlog-duo.toml`** at the repo root. Copy the shape from `references/settings-example.toml`. Fill it from the repo's own docs and scripts. Run each check command once on the main branch to confirm it passes unattended, leaves the worktree unchanged, and how long it takes. Look for files one part of the repo generates from another, such as schemas and contracts, and make sure a change to the source file triggers every check that depends on it.
2. **`docs/backlog/`** items, one file each. Add a test machine with `pair.py setup-machine <name>` only after the user approves it; it creates a dedicated checkout on that machine.

The user commits both to the main branch, or tells you to. Items not committed to the main branch are invisible to the run.

Item format, compatible with the `workflow:backlog` skill:

```markdown
---
worth: yes
added: 2026-11-02
touches: server/src/billing/**, docs/billing.md
depends: other-item-slug
issue: 22
---
# What's wrong or wanted, as a title

Enough detail for an agent who wasn't there to do it and to know when it's done.
Brief: docs/backlog/briefs/22-billing.md
```

Only `worth: yes` items run. `touches` is required: it's how overlap is judged, so list every area the work may change. `depends` and `issue` are optional.

## The loop

Repeat until `next` says stop:

```bash
pair.py next --agent <name> --wait-seconds 240
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

1. Work only in `worktree`. Read `file` from the main branch, and the brief if it names one. The brief's decisions are settled: follow them.
2. Build it, with tests, following the repo's conventions. Commit on the branch.
3. Test what you changed; see **Testing while you work**. The checks run at merge, so don't run `pair.py check` before submitting, with one exception: if your change can only be tested on a test machine, run `pair.py check <item> --agent <name>` and fix failures. There, exit 3 (machine unavailable) doesn't block you: submit anyway, since the merge can't pass without that check. Exit 4 sends the item to the user: go back to the loop. If `uncovered` is listed, or the exit is 2, escalate.
4. Run `pair.py submit <item> --agent <name>`. Mention any `undeclared` files in your summary, then return to the loop.

After submitting, the item isn't yours. Don't touch that branch again.

## Reviewing

You now own this branch, in `worktree`. The author has moved on and will not read your findings.

1. Read `file`, its brief if it names one, then the whole diff against the main branch. Check it against what the item and brief asked for. Look for wrong behavior, missing cases, missing tests, and changes the item didn't ask for.
2. Fix every real problem yourself and commit on the branch, testing your fixes as described in **Testing while you work**. Leave matters of taste alone: a different style isn't a problem, and neither is code you'd merely have written differently.
3. Run `pair.py merge <item> --agent <name>`. It rebases, runs the checks the change triggers unless that exact code already passed them, and lands the work. Exit codes:

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

## Questions

The other agent answers your questions, through the review. Don't stop to wait for an answer.

- **While building:** make the best-supported call, list it under `Calls for the reviewer:` in your commit message, and keep going.
- **While reviewing:** rule on every listed call. Keep it, or change it and fix the code. A question you have yourself is yours to settle.

A decision the brief records as the user's stands. Changing one is an escalation.

## Escalating

Escalate only when one of these is true:

- Only the user can answer: credentials, payment, or a real-world fact no agent here can test. Where other work can continue on a cautious default, take the default, record it as left for the user in the item's notes, and don't escalate.
- A correct fix would overturn a decision the brief records as the user's.
- A check can't be made to pass after a real attempt.

```bash
pair.py escalate <item> --agent <name> --reason "<what the user must decide>"
```

Then return to the loop. Never send problems back to the other agent, wait for it to fix them, or ask it to redo work.

## Testing while you work

While building, fixing or reviewing, run the smallest set of tests that covers what you changed. Use the command `repo_notes` names for running tests, if it names one. After a small fix, rerun only what that fix affects.

- A run that reports no tests, or fewer than it should, is not a pass.
- `pair.py merge` is the gate. It runs every check the change triggers, in full. Run it with nothing that narrows what a check covers, including environment variables, filters and edits to the settings.
- The full suite runs when a milestone is done, not on every merge, so a slow check is triggered only by the item that closes a milestone. If that check fails, fix it in that item, whichever earlier item caused it.

## Long commands

`check` and `merge` can take many minutes. Run them with your tool's background or long-timeout mode so a tool time limit doesn't kill them. If one is killed anyway, run it again: it waits for anything still running from the killed attempt, and a merge that already reached the main branch reports success.

A check must leave the worktree unchanged. If `check` or `merge` says a check changed files, fix the check's cause or escalate; don't commit around it.

## Finishing

When `next` says stop, run `pair.py status` and report to the user: what merged, each `needs_you` item and the decision it needs, each `held` item, each `attention` item, and anything still `active`. For each entry in `issues`, say whether it's `complete` or what remains, and list the work its brief leaves for the user. A complete issue still needs the user to push and close it. Stop there. The other agent reports on its own.

## Red flags

| Thought | Reality |
|---|---|
| "I'll send the author these findings" | No. Fix them, or escalate if they meet the rules above |
| "The other agent's task looks stuck, I'll take it" | Never take held work. It's a stop or a `status` report for the user |
| "It's waiting, so I'll grab something nearby" | Waiting is correct. Anything nearby probably overlaps |
| "The issue is vague, but the agents can work it out" | They can't ask the user once the run starts. Settle what only the user can answer first |
| "This decision is small, I'll pick for the user" | Before the run, ask if a wrong guess would matter. During it, list the call for the reviewer |
| "I'll stop and wait for the user on this question" | Only the user-only cases in **Escalating** go to them. Everything else goes to the reviewer |
| "The queue is empty, so the goal must be done" | Only the `plan-batch` item decides that, against the brief's done test |
| "This failing check is unrelated to the change" | The merge will still refuse. Fix it or escalate |
| "I'll run the full check before submitting, to be safe" | The merge runs it. Test what you changed and submit |
| "I'll narrow the check so this merge goes faster" | Never. Narrow your own test runs, not the gate |
| "A small direct commit to main is fine" | Only `pair.py merge` lands work |
| "The Mac needs disk space, I'll clear old builds" | Delete nothing. Low disk goes to the user |
