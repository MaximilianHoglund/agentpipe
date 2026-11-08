## Context

The rust stack (extracted in `agentpipe-framework-core`) shows the pattern: defaults, per-slot
env, cleanup, prompt notes, mutation. .NET differs mainly in isolation: each worktree already
has its own `bin/` and `obj/`, so there is no shared target dir to split, but MSBuild worker
nodes and the build server outlive a build and can hold files across worktrees.

## Decisions

- **Isolation by environment**: `MSBUILDDISABLENODEREUSE=1`, `DOTNET_CLI_USE_MSBUILD_SERVER=0`
  and `UseSharedCompilation=false` per slot, rather than per-slot output paths. To verify on
  Max's work solution before fixing as the default; the contract suite's parallel-gate case is
  the check.
- **Stryker.NET with `--since:<base>`** (file-level, not line-level like cargo-mutants'
  `--in-diff`), reading `mutation-report.json`. Line filtering to changed hunks happens in the
  parser, so evidence stays about the change.
- **One evidence format** across stacks so the reviewer prompt and the verdict rule ("a real
  survived mutant is a blocking coverage finding") stay stack-free.

## Risks / Trade-offs

- [Stryker.NET is slow on large solutions] → mutation off by default for dotnet, with a
  timeout, and `--test-project` scoping in the target's options.
- [Defaults that don't fit a team's solution layout] → every value is overridable, and
  `doctor` runs the gate once on the base so a wrong default shows before agents start.

## Open Questions

- Does Max's work solution run its tests with a database or other service the gate must start?
  If so, the Stack needs a `services` step (docker compose up/down) per slot.
