## Why

Not everyone wants an issue tracker or a remote to try agentpipe, and some work can't leave the
machine. A user should be able to run the whole loop (plan, implement, review, merge) with only
git and Claude Code, keeping items as files and change requests as local branches, and add a
tracker or a remote later without losing state.

## What Changes

- A `local` Tracker: work items stored as Markdown files with front matter under
  `<work_dir>/items/` (or a configured repo path, so they can be committed), with statuses,
  comments, kinds and blocked-by links. Humans answer decisions by editing the file or with
  `agentpipe item` commands.
- A `local` CodeHost: change requests are branches in the user's repository, with a JSON
  record per change request (base, head, body, verdicts, comments) in the work dir. Merging
  squash-merges into the local base branch; nothing is pushed unless `push = true`.
- `agentpipe item list|show|comment|status|approve` for working with local items, and
  watcher support for answering a decision without leaving the pane view.
- `agentpipe export --to <tracker>` copies local items, statuses, comments and links into a
  real tracker and switches the target over.

## Capabilities

### New Capabilities
- `local-tracker`: file-based work items, statuses, comments and links, and the CLI for them.
- `local-code-host`: change requests as local branches with review records, and local merges.

### Modified Capabilities

## Impact

- New adapters under `agentpipe/adapters/local/`; CLI commands; watcher keys for decisions.
- Depends on `agentpipe-framework-core` (ports), `agentpipe-configuration` (stage choice) and
  `agentpipe-integration-kit` (contract suites).
