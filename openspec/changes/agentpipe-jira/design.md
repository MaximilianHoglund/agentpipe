## Context

Jira statuses belong to a per-project workflow; the API cannot set a status directly, only run a
transition available from the current one. Teams also often refuse new workflow states for a
tool. The ten internal statuses therefore need a mapping that can mix real states with labels.

## Decisions

- **Map to states where they exist, labels where they don't.** A team keeps its workflow; the
  pipeline's extra states (blocked by task, awaiting task, needs decision) ride on labels.
  Reading a status back takes the label first, then the state.
- **Transition search, not ids in config.** Users name target states; the adapter finds the
  transition each time, so workflow edits that keep state names don't break it.
- **Cloud v3 by default, v2 for Data Center**, auth email + API token (Cloud) or PAT (DC), both
  from env or keyring.
- **Built through the kit**, so this change also tests the kit on a hard case; gaps found in
  the kit are fixed there, not worked around here.

## Risks / Trade-offs

- [Workflows with required fields on transitions (e.g. resolution, assignee)] → per-transition
  field defaults in the map; doctor reports transitions with required fields not covered.
- [Teammates move pipeline issues by hand into unmapped states] → an unmapped state reads as
  needs decision and is logged, never crashes sync.

## Open Questions

- Max's work Jira: Cloud or Data Center, the project's workflow states, and whether a bot
  account is allowed.
