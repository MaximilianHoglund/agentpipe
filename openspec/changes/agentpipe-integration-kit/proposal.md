## Why

agentpipe should be a framework that each person shapes to their own tools, not a product that
ships every integration. A user with Linear, Azure DevOps, Gitea or a home-grown tracker should
be able to ask an agent to walk them through setup and build the adapter they need, and know it
works because it passes the same tests the built-in adapters pass.

## What Changes

- Contract test suites for every port (Tracker, CodeHost, TaskSource, Stack, Gate,
  MergePolicy), runnable against any adapter: `agentpipe adapter test <kind>/<name>`. They run
  against an in-memory fake, recorded fixtures, or a live sandbox the user names.
- `agentpipe adapter new <kind> <name>`: scaffolds a plugin (class skeleton with every method,
  option schema, a contract-test hookup and a README stub) in the user's or repo's plugin dir.
- `agentpipe setup`: starts an interactive Claude Code session with a setup guide. The agent
  interviews the user (tracker, host, git remote, stack, gate, approval, task sources), picks
  built-in adapters where they fit, writes the config, and for anything missing scaffolds and
  builds an adapter, runs its contract tests against the user's sandbox, then runs `doctor`.
- Recorded fixtures: `--record` captures an adapter's HTTP or CLI traffic against a sandbox
  into replayable fixtures (secrets scrubbed), so the adapter's tests run offline afterwards.
- Port reference docs generated from the protocols, and an authoring guide with the GitHub and
  local adapters as worked examples.

## Capabilities

### New Capabilities
- `adapter-contract-tests`: the per-port contract suites, how they run, and record/replay.
- `adapter-scaffolding`: generating a new plugin that already wires into the registry and tests.
- `guided-setup`: the agent-led setup session and what it must produce.

### Modified Capabilities

## Impact

- New `agentpipe/contract/`, `agentpipe/scaffold/`, a setup prompt and docs.
- Depends on `agentpipe-framework-core` and `agentpipe-configuration`. Later adapter changes
  (local, GitLab, Jira, stacks) are built and verified with this kit.
