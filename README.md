# agentpipe

A multi-agent pipeline that takes OpenSpec task lists to merged code without a human
in the loop, unless a task needs a decision.

```
tasks.md ──planner──▶ dependency plan ──sync──▶ GitHub issues (status labels + native "blocked by")
                                                   │
             pending implementation ◀──────────────┘
                     │ implementer agent (own worktree, own cargo target dir)
                     ▼
              PR opened ── pending approval ── reviewer agent (watches GitHub PRs)
                     ▲                              │
     changes requested ◀── reject (≤ max_rounds) ───┤
                                                    │ approve
                                                    ▼
                         approved ── merge (base merged in, gate re-run) ── finished
                                                                     └── tick tasks.md, unblock dependents
```

## Targets

Each `[targets.<name>]` in `pipeline.toml` is one project: repo, GitHub remote, the
OpenSpec changes it covers, the project dir and its test gate. Run one at a time:

```powershell
cd E:\Source\Claude\pipeline
uv run agentpipe targets
uv run agentpipe plan dota          # infer blockers; prints the graph; no GitHub writes
uv run agentpipe sync dota --dry-run
uv run agentpipe sync dota          # create issues, labels, blocked-by links
uv run agentpipe run dota           # dispatcher in this console: implement, review, merge, repeat
uv run agentpipe start dota         # the same, hidden, plus a watcher window; closing it stops the dispatcher
uv run agentpipe stop dota          # background one: finish running agents, then exit (--now kills)
uv run agentpipe watch dota         # live panes: one scrollable pane per running job, plus the dispatcher log
                                    #   shows agents' reasoning; each run of tool calls is one summary line
                                    #   t: tool detail, tab: next pane, m: maximize it (m/esc restores), q: quit
                                    #   --plain: the old redrawn text view; --once: one text snapshot
uv run agentpipe denials dota       # review tool calls agents were refused
uv run agentpipe status dota
```

`run` also syncs every `sync_seconds`, so `sync` on its own is only for looking first.
Ctrl-C (for `run`) or `stop` (for `start`) stops scheduling and waits for running agents;
everything resumes on restart. Closing the watcher window that `start` opened acts like
`stop` once no such window is left; `start --no-watch` and a plain `watch` aren't tied to
the dispatcher. A hidden dispatcher's console output and any crash
traceback go to `logs/dispatcher.out`, and crashes are also logged to `pipeline.log`.

The watcher's "N waiting on you" counts issues in `needs decision` and unworked ticket
proposals. It leaves out a target's `parked` items (issue numbers or task keys) and its
milestones' `deferred` tasks; their labels stay as they are.

Optional per target: `extra_tools` (added to the implementer's and reviewer's allowed tools)
and `env` (extra environment variables; `{NAME}` in a value expands to the inherited value).

## Milestone releases

A target can work on a development branch (`base = "dev"`) and release to another
(`release_branch = "main"`). Each `[[targets.<name>.milestones]]` names a tag, a title and the
OpenSpec changes it needs. After every sync, if the next unreleased milestone has all its
changes ticked off (or archived) on `base`, the dispatcher:

1. finds the commit that ticked its last task, so later work stays out of the release;
2. merges it into `release_branch` with `--no-ff` and runs the test gate on the merge;
3. optionally runs `release_build` and collects `release_assets` (globs, project-relative);
4. pushes the release branch and an annotated tag, then creates a GitHub release.

Milestones are released strictly in order, and a pushed tag marks one as done. If the merge,
gate or build fails, it files one `needs decision` issue (later failures comment on it) and
retries from the head of `base` once that moves. Humans commit fixes to `base`, not to the
release branch.

## Statuses (issue labels)

| Label | Meaning | Who moves it on |
|---|---|---|
| `status: blocked by task` | A blocker is unfinished (see the issue's blocked-by links) | sync |
| `status: needs decision` | Needs information or a decision; the question is in the latest comment | **you** |
| `status: awaiting task` | Decided, but waits for other issues (its blocked-by links) to be done | dispatcher |
| `status: pending implementation` | Unblocked, waiting for a free agent | dispatcher |
| `status: in progress` | Implementer working | implementer |
| `status: pending approval` | PR waiting for the reviewer, **or** an agent-filed ticket waiting for you | reviewer / **you** |
| `status: changes requested` | Reviewer rejected; implementer revising | implementer |
| `status: approved` | Being merged | dispatcher |
| `status: finished` | Merged, issue closed | — |

**Answering a decision:** comment on the issue, then set `status: pending implementation`.
Every non-pipeline comment on the issue and its PR is given to the agent as
authoritative input. The planner's "needs decision" verdict stays overridden after that.

**Waiting for other work:** `agentpipe wait <target> <issue> <blocker>...` parks a decided
issue until its blockers are done. Blockers are issue numbers or task keys (`crafting/3.5`).
A task key must already have an issue: wait only for defined work. The command adds a
blocked-by link per blocker, so GitHub shows the backlink ("blocking") on the blocker. It
comments on both sides and sets `status: awaiting task`. When an issue closes, the
dispatcher checks the issues it blocks. A waiter goes to `pending implementation` once its
last blocker is done; a waiter with several blockers waits for all of them. Each sync also
sweeps every waiter, which catches issues closed by hand. If a blocker is closed as not
planned, or a waiter has no blockers, the waiter goes to `needs decision`. An approved
ticket whose text names unfinished tasks is registered the same way.

**Agent-filed tickets:** implementers and the reviewer report defects and improvements
they notice outside their scope. They arrive as issues labelled `kind: defect` or
`kind: improvement` in `status: pending approval`. Approve by setting `status: pending
implementation` (the ticket body is the spec); reject by closing it.

## The review gate

Before the reviewer runs, the target's `test` command must pass on the PR head
(failure is an automatic rejection, no tokens spent). The reviewer then checks spec
conformance, cut corners, duplication (it searches for existing code the diff
re-implements), architecture, tests-of-implementation, and happy-path-only coverage,
and makes at least three deliberate breaks in the code to see whether the tests catch
them. Code enforces two rules on its verdict: any blocking finding means rejection,
and an uncaught, behaviour-changing break becomes a blocking coverage finding.
For targets with `mutation` set (dota and arpg), cargo-mutants also mutates the PR's
changed lines and the reviewer gets the `MISSED` mutants as evidence; each real one is
a blocking coverage finding. It runs in place in the disposable review checkout,
reusing the gate's build, and the checkout is reset afterwards.

After `max_rounds` rejections a task goes to `needs decision`. Merge conflicts with
the base branch are sent back without using up a round.

## Model tiers

An agent in `pipeline.toml` may set `light_model` beside `model`. The planner rates each
task `light` (routine, fully specified) or `standard`; tasks planned earlier get rated in
the same extra run that predicts their `touches`, on the planner's light model. Tickets
aren't planned: once approved, and before their first run, they're rated in one run per
sync on the planner's light model, and the tier goes in `state.json`. The
implementer's light model takes the first implementation of a light task (a batch only if
every member is light) and every conflict-resolution and cleanup run. Revisions always run
on the full model; so does every run after a task escalates, which happens when a light
run hits its turn cap or timeout (its WIP is saved and the full model redoes it at once),
a light conflict run leaves the merge unresolved (the full model tries once more), or the
reviewer rejects a light task's PR. The reviewer has no light model: it is the quality
gate. A session is resumed only by the model that started it. The log says
`... moving to <model>: <why>` on each escalation; `state.json` keeps the reason.

## Keeping merge conflicts down

Parallel PRs into one base conflict, and each conflict used to cost a full revise and a
full re-review. Four things cut that:

- **Scheduling by files.** The planner predicts each task's `touches` (repo-relative
  files or directories; tasks planned earlier get theirs in one extra planner run). A
  task in `pending implementation` waits while another task in flight (being
  implemented, or with an open PR not parked in `needs decision`) touches the same
  files, by prediction or by the PR's actual changed files. Tickets use the files their
  body names. Paths in the target's `overlap_ignore` don't count. Work already under
  way always resumes. The log says `... waits for <key> (PR #n) to merge first`.
- **Mechanical resolution.** Merges use diff3 markers. A conflict where both sides only
  added lines (a `mod` line, a decision at the end of a doc) is resolved by keeping
  both, then the target's `format` command runs. A real conflict goes to an agent.
- **rerere** is enabled on the repo, so a branch that merges its base again replays
  earlier (agent-made, tested) resolutions.
- **Light updates.** An approved PR that conflicts gets no full revise: a clean or
  mechanical merge (gated) is pushed straight back to review, a real one gets a short
  conflict-only agent run. The re-review then sees only the merges' resolutions
  (`git show --remerge-diff`), with no mutation run; a clean merge is approved without
  an agent. New commits besides merges get the full review.

Implementers are also told not to number new entries in shared logs, since parallel
PRs claim the same number.

## What runs where

- Agents run as headless `claude -p` on your subscription, with `--permission-mode
  dontAsk` and the tool allowlists in `pipeline.toml`. They can edit and commit in
  their worktree but cannot push, call `gh`, or touch other branches; the dispatcher
  does all of that.
- Work dirs: `E:\Source\.pipeline\<target>\` holds `base\` (a clean checkout of
  `origin/main`), `wt\<task>\` (implementer worktrees), `review\` (disposable PR
  checkouts), `target\<slot>\` (cargo target dirs, reused per slot), `logs\` (every
  prompt and raw agent output), `plan.json` and `state.json`.
- Agents work from `origin/main`. Uncommitted work in your own checkout is invisible
  to them; push what they should build on. The dispatcher pushes merges and tasks.md
  ticks to `main`, so pull afterwards.
- Usage or capacity limits pause the dispatcher for `limit_backoff_seconds`; the
  interrupted agent resumes its session afterwards.

## Setup

1. `gh` is installed at `E:\Programs\GitHubCLI\bin\gh.exe` (on the user PATH; winget is not available here). Run `gh auth login` once.
2. Branch protection on `main` must not require an approving review: all agents act as
   your GitHub account, and GitHub doesn't let an account approve its own PR. The
   reviewer posts its verdict as a PR comment instead.
3. `uv run agentpipe sync <target> --dry-run`, then `run`.

## Tests

`uv run pytest` — parsing, the dependency graph, sync decisions, verdict rules, ticket
filing and the scheduler (against an in-memory GitHub).
