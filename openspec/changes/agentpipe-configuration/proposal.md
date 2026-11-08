## Why

Each user composes their own pipeline: which tracker, if any; whether git has a remote; where
the gate runs; whether a human must approve before merge. Today one `pipeline.toml` mixes Max's
personal targets, machine overrides and shared settings, and its options assume GitHub. Someone
else can't adopt it without editing Max's file, and nothing checks that their choices fit
together.

## What Changes

- Layered configuration: a user file (`~/.config/agentpipe/config.toml`: binaries, accounts,
  machines, models, limits, deny rules), a repo file (`.agentpipe.toml`, checked in: stages,
  stack, commands, context docs, task source), and per-machine overrides. Later layers win, and
  `agentpipe config show <target>` prints the merged result with where each value came from.
- Pipeline composition: per target, pick an adapter for each port (`tracker`, `code_host`,
  `task_source`, `stack`, `gate`, `merge_policy`, `runtime`), including `none` or `local`
  where the stage is optional, with each adapter's own options in its table.
- Approval policy: after the reviewer agent approves, either merge (agent approval is enough),
  record the agent approval on the host (e.g. a GitLab approval from a reviewer identity) and
  merge once the host's rules are met, or hand the change request to named humans and wait.
- Identities: the dispatcher, implementer and reviewer can act as different accounts or tokens,
  so the host shows who did what and the reviewer can approve someone else's change request.
- Validation: unknown keys, impossible combinations (merge batches without a remote, native
  links on a tracker without them) and missing credentials fail at load with a fix suggestion.
- `agentpipe init` writes starter files from answers; `agentpipe doctor` checks binaries,
  credentials, adapter connectivity, the gate and agent permissions (absorbs `preflight`).
- `pipeline.toml` keeps loading through a converter; `agentpipe config migrate` rewrites it.

## Capabilities

### New Capabilities
- `layered-config`: the three config layers, precedence, provenance and validation.
- `pipeline-composition`: choosing an adapter (or none) per stage, and the combinations allowed.
- `approval-policy`: what happens between agent approval and merge, and which identity acts.
- `setup-commands`: `init`, `doctor`, `config show` and `config migrate`.

### Modified Capabilities

## Impact

- `config.py` rewritten around the registry from `agentpipe-framework-core`; `cli.py` gains
  commands; the preflight code moves into `doctor`.
- Max's `pipeline.toml` is migrated into a user file plus one `.agentpipe.toml` per repo.
- Depends on `agentpipe-framework-core`.
