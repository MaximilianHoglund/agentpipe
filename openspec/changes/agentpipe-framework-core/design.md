## Context

agentpipe is ~5.5k lines of Python plus 3.4k of tests in `E:\Source\Claude\pipeline`.
`pipeline.py` (2161 lines, one class) calls a single `GitHub` class (`gh` CLI) at 68 sites and
assumes issues and PRs share numbers, status is a label, blocked-by links are native and PR
bodies close issues with `Closes #n`. OpenSpec is assumed in `tasks.py`, `Target.changes_dir`,
milestones, archive and prompts. cargo is assumed for slot isolation and workspace cleanup.
The test suite's `FakeGitHub` is already a de facto interface. Max runs three live targets on
two machines; the refactor must not break them.

## Goals / Non-Goals

**Goals:**
- Ports with small, typed contracts; today's behaviour as the first adapters.
- Adapters a user (or an agent working for them) can write outside agentpipe's repo.
- Zero behaviour change for existing targets, proven by tests and dry-run parity.

**Non-Goals:**
- New adapters (GitLab, Jira, local, .NET): later changes.
- The new config format: `agentpipe-configuration`. This change keeps `pipeline.toml` working.
- Replacing Claude Code as the agent runtime. The port exists so accounts and accounting vary,
  not to support other agent CLIs yet.

## Decisions

- **Protocols plus a registry, not inheritance.** Ports are `typing.Protocol`s in
  `agentpipe/ports/`; adapters register with a decorator (`@register("tracker", "github")`).
  Alternative: abstract base classes. Protocols let plugin authors write plain classes and let
  the contract tests do the checking.
- **Typed ids.** `ItemRef` and `ChangeRef` are distinct types carrying the adapter's native id
  and display form. Removes the shared-number assumption at compile-check time (mypy) rather
  than by convention.
- **Status stays internal.** The ten statuses live in the core; a Tracker maps them to its own
  representation (labels, workflow states, a file field). The mapping is the adapter's job.
- **Links capability with a core fallback.** Native blocked-by links are used when the Tracker
  declares `links`; otherwise the core owns the edges (it already has them in `plan.json`).
- **Capabilities, not feature flags.** Each adapter declares what it can do (`links`, `labels`,
  `releases`, `approvals`); the core branches on capabilities, never on adapter names.
- **Split `pipeline.py` along the ports while moving calls**: sync, dispatch, implement,
  review, merge and release become modules. This is the one moment the file is rewritten
  anyway; doing it later costs a second full conflict window.
- **Strangler order**: introduce ports and the GitHub adapter behind them first with tests
  green, then move OpenSpec, then the stack, then accounting. Each step merges on its own.
- **Repo split with history** (`git filter-repo --subdirectory-filter pipeline`), done first,
  so every later task lands in the new repo.

## Risks / Trade-offs

- [The refactor collides with ongoing tuning of `pipeline.py`] → start only when no other
  session has uncommitted agentpipe work; keep the window short; land in small merges.
- [Subtle behaviour drift in sync or merge logic] → dry-run parity check against each live
  target before and after, plus the existing 1,000+ lines of scheduler tests.
- [Plugin API frozen too early] → contract version numbers from day one; only Max's adapters
  exist until `agentpipe-integration-kit`, so breaking v1 is cheap.
- [Running dispatchers read prompts from disk and crash on template changes] → stop, upgrade,
  start; never hot-swap.

## Migration Plan

1. Stop all dispatchers (desktop arpg, laptop dota and riftbound) after their running jobs.
2. Split the repo; install the package on both machines; point `laptop-run.sh` at it.
3. Start each target on the new code; check the log for resumed jobs.
Rollback: the old checkout stays in the monorepo until all three targets have run a day.

## Open Questions

- Does the work Claude account expose usage windows to Claude Code (`rate_limit_event`), or
  only cost? Decides the default accounting mode at work.
