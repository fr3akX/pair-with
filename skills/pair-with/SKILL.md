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
5. Start the other agent, using the args from **Choosing the other agent**. If `HC_TERMINAL_ID` isn't set, stop and tell the user this skill must be started inside a herdr-core terminal. Label yourself too, so every agent of the run can be found by name. The `herdr.space`/`herdr.tab` labels put the new agent in a pane of your tab, and its prompt goes in as its first message.
   ```bash
   L=$(hc get "$HC_TERMINAL_ID" | jq -r '"--label herdr.space=\(.labels["herdr.space"]) --label herdr.tab=\(.labels["herdr.tab"])"')
   hc label "$HC_TERMINAL_ID" pair.agent=<your name>
   hc run --bg --no-parent --keep --name pair-<name> --cwd "$PWD" --label pair.agent=<name> $L -- <kind> <args> "Use the pair-with skill. You are joining a run as agent '<name>'. Go straight to The loop."
   ```
   Don't wait for it: you start working straight away too. If `hc run` fails or `hc get pair-<name>` doesn't show it `live`, stop and tell the user.
   Any agent of the run is addressed by its label: `$(hc ls --json --label pair.agent=<name> | jq -r '.terminals[0].address')`, written `<address of name>` below.
6. If the user asked for a watch, such as "pair with codex astra high, watched", start it as **Watching a run** describes.
7. Go to **The loop**.

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
3. Otherwise write the next 3 to 6 items, using the rules in **Turning an issue into items**, plus a closing check item and its lookahead items, both depending on all of them (see **Closing checks** below), plus `plan-batch-<n+1>` depending on everything in the batch.

If the brief has a `## Priorities` section, write items for its P0 entries first, then P1. Take P3 work only when no P0 or P1 entry has work an agent can do. Give each item the `priority` of the highest entry it moves.

**Reviewing a `plan-batch-<n>` item.** Check that the measurement is true, that each item moves the goal forward, that the items follow the brief's priorities, that the batch has the closing check and lookahead items **Closing checks** calls for, and that nothing repeats work already done or waits on the user. Fix the plan yourself, then merge.

**Closing checks.** Every batch ends with one check item, and the brief records how often the full suite runs. Unless the user picks another interval, a full integration closes every third batch and a smoke item closes the two between. Name them `batch-<n>-check-integration` and `batch-<n>-smoke`.

- **Smoke item.** Build the main branch as merged, start the app the way a user would on a test copy of the data, use each main screen once, save and reload once, and check for errors in the logs and the browser console. Record what ran. It adds no regression tests to the full suite, retakes no recorded results, and changes no file that triggers the full suite. Aim to finish within an hour, and fix any regression it finds in the smoke item.
- **Full integration.** Add the regression tests from every item since the last full integration to the full suite, then run the full suite.
- **Retire covered records.** When a newer passing test or recorded result covers the same behaviour as an older one, take the older one out of the suite instead of updating or retaking it. Keep its files, and in the evidence name each retired record and what now covers it. This isn't narrowing a check. The reviewer confirms each one and restores any whose behaviour nothing else covers.
- **Counting progress.** After a smoke item, a unit of progress counts as done only if every check its done test names ran in full during that batch. Otherwise it waits for the next full integration.
- **Lookahead items.** One agent takes the closing check, so give the other one work. Write two lookahead items in a batch that ends with a full integration, and at most one small one in a smoke batch. Each depends on the batch's other items, as the closing check does, so it opens when the closing check does. It prepares later work without needing the closing check's result, for example by reading code, capturing reference behaviour or drafting a design for a hard unit, and records what it found. It doesn't touch anything the closing check touches. Size it to about the closing check's length, and make the next `plan-batch` item depend on it.
- **Reminders.** The watch may send an agent a one-line reminder that quotes one of these testing rules. Treat it as that rule, and carry on.

A goal never widens what's off limits. Deploying, pushing and anything else reserved for the user stay reserved.

## Re-prioritizing

Use this when the user asks to re-prioritize the work, or to pause a run and re-prioritize. Do it with the user, outside the loop, from your own session or the run's first agent.

1. **Pause.** `pair.py pause` holds every `plan-batch` item, so a goal run finishes its current batch, then stops. `pair.py pause --now` holds every unclaimed item instead, so agents finish only what they hold or have awaiting review. Agents stop by themselves when `next` says the run is paused; don't prompt or stop them.
2. **List what's left.** In a goal run, list the units the brief measures progress by, such as checklist rows that aren't done. Otherwise list the open items. For each, give what's left in one to three lines and what it needs done first: an item's `depends`, or for goal units, the dependencies you read from the evidence. Mark anything only the user can do.
3. **The user picks.** P0 first, then P1. Everything else is P3. Then raise every dependency of a P0 or P1 entry to that entry's level, and their dependencies in turn. Show the user what moved up and which entry moved it. Where your harness can publish an interactive page, offer one with a P0/P1/P3 switch on each entry that raises dependencies as the user clicks. Otherwise use a table. Make the changes they ask for before writing anything.
4. **Record it** once `status` shows nothing active. Approval covers one commit on the main branch, made as **Turning an issue into items** step 6 says. Set `priority: P0` or `priority: P1` on each open item the user ranked that way; P3 items need no field. In a goal run, the entries are usually units with no item yet, so also add a dated `## Priorities` section to the goal brief that lists P0, P1 and P3, marks which entries moved up and why, and gives any reasons the user stated. Planners turn that section into items.
5. **Resume.** Run `pair.py resume`, then prompt each agent with `hc send <address of name> "Continue the pair-with loop as agent '<name>'. Go straight to The loop."`.

`next` claims items by priority, P0 first, then by `added`. An item counts at the highest priority of anything that depends on it, so a P0 item's prerequisites run before other P1 work.

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

Only `worth: yes` items run. `touches` is required: it's how overlap is judged, so list every area the work may change. `depends`, `issue` and `priority` (`P0` to `P3`, see **Re-prioritizing**) are optional.

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
- The full suite runs when a milestone is done, not on every merge, so a slow check is triggered only by the item that closes a milestone. In a goal run, that's a full integration, as **Closing checks** says. If that check fails, fix it in that item, whichever earlier item caused it.

## Long commands

`check` and `merge` can take many minutes. Run them with your tool's background or long-timeout mode so a tool time limit doesn't kill them. If one is killed anyway, run it again: it waits for anything still running from the killed attempt, and a merge that already reached the main branch reports success.

A check must leave the worktree unchanged. If `check` or `merge` says a check changed files, fix the check's cause or escalate; don't commit around it.

## Finishing

When `next` says stop, run `pair.py status` and report to the user. If `next` said the run is paused, say so first: the run isn't finished, and the user resumes it. Then report what merged, each `needs_you` item and the decision it needs, each `held` item, each `attention` item, and anything still `active`. For each entry in `issues`, say whether it's `complete` or what remains, and list the work its brief leaves for the user. A complete issue still needs the user to push and close it. Stop there. The other agent reports on its own.

## Reporting

`pair.py report [--since 24h|7d|<ISO date>]` says who did what and where the time went. It's read-only, so anyone can run it at any time, including the user. For each agent it shows items built and reviewed, the commits and lines it wrote as builder and as reviewer, its median build and review hours, and its hours waiting and running checks. It also shows each check's runs, failures and median minutes, the time merges spent waiting for each other, and for goal runs, each batch's hours and its three slowest items. `report_exclude` in the settings leaves paths such as generated data out of the line counts.

Use it when the user asks how the run is going or who did what. Items merged before this was recorded have no times, and `timed` says how many do.

## Watching a run

A watch is an optional third agent that keeps an unattended run moving. It checks the run on a timer, unblocks what it safely can, and keeps a log for the user. It never mediates: anything the two agents escalate stays with the user.

**Starting it.** The first agent starts the watch after the other agent, in a new pane of its tab. It's always Claude, because it relies on `/loop`. Its args come from `[watch]` in the settings, default `["--model", "sonnet"]`, and its interval comes from `every_minutes`, default 60.

```bash
L=$(hc get "$HC_TERMINAL_ID" | jq -r '"--label herdr.space=\(.labels["herdr.space"]) --label herdr.tab=\(.labels["herdr.tab"])"')
hc run --bg --no-parent --keep --name pair-watch --cwd "$PWD" --label pair.agent=watch $L -- claude <watch args> "/loop <every_minutes>m Use the pair-with skill as this run's watch. Do one round of Watching a run."
```

**One round.** The watch acts on its own, so it never asks a question or waits for an answer.

1. Read: `pair.py status`; `hc ls --json --label pair.agent=<name>` for each agent; for each, `hc get <address>` (`.status.state`) and `hc read <address> --lines 60`; `ps -Ao pid,etime,command` filtered to `pair.py` and the repo's check commands; the log below.
2. Decide whether each agent is **stalled**. It is only if all of these are true: `hc get` shows its state `idle` or `done` (`done` is a turn that ended unwatched), its pane shows a finished turn at an empty prompt, no background shell or task of its is running, and no `pair.py` process of its is running. Waiting on `next`, a long check, or a silent hour isn't a stall. An agent paused by a usage limit isn't stalled until the limit resets.
3. Act, using only these:
   - **A stalled agent with work left in `status`:** `hc send <address> "Continue the pair-with loop as agent '<name>'. Go straight to The loop."` without `--wait` or `--if` (`--if idle` refuses a `done` agent), at most twice per agent per item, counted from the log. After that, record it for the user.
   - **An agent paused on an approval prompt:** approve once, never with an "always allow" option, but only for ordinary build, test, git or file work inside the repo or its worktrees for its item. Leave the prompt alone and record it if it deletes anything outside its worktree, pushes, deploys, uses credentials, touches anything named in `off_limits`, or does anything you can't tell.
   - **An agent's process or pane is gone:** start one replacement as **Starting a run** step 5 does, with the prompt above. Do this once per agent per day, and only after checking that no agent with that name is alive.
   - **An agent breaking a testing rule its pane shows:** send one reminder with `herdr agent prompt pair-<name> "<text>"`, without `--wait`, once per reminder per item, counted from the log. Never send one while the agent is waiting on an approval prompt. Use only these texts:
     - when it reruns a whole set of tests or workflows after a fix that touched only some of them: "Reminder from the skill: after a small fix, rerun only what that fix affects, not the whole set."
     - when a smoke item adds regression tests, retakes recorded results or changes what triggers the full suite: "Reminder from the skill: a smoke item adds no regression tests, retakes no recorded results and doesn't change what triggers the full suite."
     - when a full integration updates or retakes an older record that a newer passing one covers: "Reminder from the skill: retire an older record that a newer passing one covers, instead of retaking it."
4. **Watch the pace.** Run `pair.py report --since 24h` and compare it with the whole run's `pair.py report`. Record a slowdown when any of these is true:
   - the last batch's `hours` is more than 1.5 times the median of the three batches before it;
   - one of its `slowest` items took more than half the batch;
   - a check's `median_minutes` over the last day is more than 1.5 times its median for the whole run, or its runs fail more than a third of the time;
   - `merge_lock_wait_hours` or an agent's `waited_hours` over the last day is more than 2;
   - an item has been `in_progress` or `reviewing` more than twice the agents' `median_build_hours` or `median_review_hours`.

   For each, name the item or check, the numbers, and the likely cause from the agent's pane, such as a check rerun after failures, many recorded results being retaken, or work queued behind the shared check lock. Then name the change that would help, such as a longer full-integration interval in the brief, retiring covered records, or splitting the item. These are the user's decisions, so record them and don't act on them.

   When a goal batch closes, also record each agent's idle hours in it (`waited_hours` for that span) and whether it had the lookahead items **Closing checks** asks for.
5. **Forecast usage.** Read each agent's usage from its pane, such as the weekly percentage left in Codex's footer or a limit message in Claude's, and log it. From the log, work out how much a batch uses. Tell the user once each when an agent falls below 15% and 10%, and when the forecast shows it running out before the current batch's closing check and plan merge. Running out pauses that agent until the limit resets, which the user may prefer to plan for.
6. **Watch the result, in a goal run.** When a batch's progress fold merges, log the counts the brief measures. If it adds nothing that counts as done, count the batches in a row without any. From the second such batch on, tell the user, with the units the evidence names as closest to done and what each still lacks. This is separate from the stop rule in **Running toward a goal**, which only looks for batches that verified nothing new.
7. Append one line to `<git common dir>/backlog-duo/watch.log`: the time, merged count, active and open items, each agent's state and usage, what you did, and anything from steps 4 to 6.

Never run `next`, `submit`, `check`, `merge`, `release`, `escalate` or `requeue`. Never rule on a `needs_you`, `held` or `attention` item. Never edit code, settings, items or state, commit, kill a process, delete anything, start more agents than step 3 allows, or send an agent any text other than the ones step 3 gives.

An agent that stopped because the run is paused isn't stalled. Never run `pause` or `resume` yourself.

**Ending.** End when both agents have stopped and either `status` shows nothing active or open with no `plan-batch` item left on the main branch, or `status` shows `paused` with nothing active. Write a final summary to the log and the pane. Cover what merged, `pair.py report` for the run, where the time went, idle agent-hours, reminders sent, any slowdowns recorded, each agent's usage left, each `needs_you` item with the exact decision it needs, and every action the watch took. For a pause, also say the run is waiting for the user to re-prioritize and resume it. Then end the loop.

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
| "The run is paused, I'll resume it so work continues" | Only the user resumes a pause |
| "This P3 item is quicker, I'll plan it ahead of P0 work" | Plan by the brief's priorities. P3 waits until no P0 or P1 work can run |
| "This failing check is unrelated to the change" | The merge will still refuse. Fix it or escalate |
| "I'll run the full check before submitting, to be safe" | The merge runs it. Test what you changed and submit |
| "I'll narrow the check so this merge goes faster" | Never. Narrow your own test runs, not the gate |
| "A small direct commit to main is fine" | Only `pair.py merge` lands work |
| "The Mac needs disk space, I'll clear old builds" | Delete nothing. Low disk goes to the user |
| "As the watch, I can settle this escalation" | Never. The watch unblocks; decisions stay with the user |
