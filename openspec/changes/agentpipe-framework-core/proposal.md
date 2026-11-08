## Why

agentpipe works only for Max's own setup: GitHub as both tracker and code host, OpenSpec as the
only task source, Rust/cargo build isolation, Max's machine paths and his name in prompts. He
wants it at work (GitLab Premium, a work Claude account, .NET) and wants friends and colleagues
to run it on their own stacks. Every later change (configuration, local mode, GitLab, Jira,
stacks, intake) needs stable extension points, so the core comes first.

## What Changes

- Move agentpipe out of the `E:\Source\Claude` monorepo into its own repository and package,
  installable with `uv tool install`. The laptop's clone and `laptop-run.sh` follow.
- Define the ports the core talks to: **Tracker** (work items, statuses, comments, blocked-by
  links), **CodeHost** (change requests, merge, releases), **TaskSource** (where planned work
  comes from and what "done" means), **Stack** (build commands, per-slot isolation, cleanup),
  **Gate** (where the test gate runs), **MergePolicy** (what follows an approval), and
  **AgentRuntime** (how an agent runs and how its usage is accounted).
- A plugin registry: built-in adapters and adapters found in a user's plugin directory or
  through Python entry points, selected by name in config.
- Re-home today's behaviour as the first adapters: `github` (Tracker and CodeHost), `openspec`
  (TaskSource), `rust` and `godot` (Stack), `local-command` (Gate), `auto-merge` (MergePolicy),
  `claude-code` (AgentRuntime, with subscription-window or cost-budget accounting).
- Remove personal assumptions from code and prompts: the `E:/` default work root, "Max",
  "a player", the Windows `python` stub note become config values or stack prompt notes.
- No behaviour change for Max's existing targets: the same `pipeline.toml` keeps working
  through a compatibility loader until `agentpipe-configuration` replaces it.

## Capabilities

### New Capabilities
- `extension-ports`: the port contracts the core depends on, and the rule that the core
  names no vendor, tool or stack.
- `plugin-registry`: how adapters are discovered, registered, selected and versioned.
- `usage-accounting`: how agent usage is measured and throttled, by subscription windows or
  by cost budget.
- `standalone-package`: agentpipe as its own installable repository.

### Modified Capabilities

## Impact

- `agentpipe/pipeline.py` (2161 lines, 68 GitHub call sites), `github.py`, `config.py`,
  `tasks.py`, `deps.py`, `milestones.py`, `syncplan.py`, `claude.py`, every prompt.
- Tests: `FakeGitHub` becomes fakes of the Tracker and CodeHost ports.
- Run data under `<work_root>/<target>/` keeps its layout; `state.json` gains a schema version.
- Must start only after the session editing `config.py` and `pipeline.py` (2026-11-08) has
  landed, and should finish quickly: it touches nearly every line that calls GitHub.
