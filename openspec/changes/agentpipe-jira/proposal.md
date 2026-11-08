## Why

Max's work tracks tasks in Jira, alongside GitLab for code. Jira is also the hardest tracker to
map: statuses are workflow states reached by transitions, not labels, and each project's
workflow differs. Building it with the integration kit proves the "bring your own tracker" path
on a real, awkward case, and ships a reference that colleagues on Jira can use as is.

## What Changes

- A `jira` Tracker (Jira Cloud REST v3, Data Center REST v2 as an option): issues created in a
  configured project and issue type, labelled `agentpipe`, comments in Atlassian Document Format
  (plain text on v2), "Blocks" issue links, close by transition with a resolution.
- A status map in config: each internal status maps to a workflow status (reached by finding a
  transition to it) or, where the workflow has no matching state, to a label on top of the
  nearest workflow status. `agentpipe doctor` checks every mapped transition exists.
- Code-host linking: branch names and merge request titles carry the Jira key, so GitLab's
  Jira integration links them, with no dependency on that integration being enabled.
- Built with `agentpipe-integration-kit`: scaffolded, contract-tested against a Jira Cloud
  sandbox, fixtures recorded.

## Capabilities

### New Capabilities
- `jira-tracker`: Jira issues as the tracker, with workflow-aware status mapping and links.

### Modified Capabilities

## Impact

- New `agentpipe/adapters/jira/`; task and change-request naming takes a tracker-supplied
  branch prefix.
- Depends on `agentpipe-framework-core`, `agentpipe-configuration` and
  `agentpipe-integration-kit`.
