## Context

Two intake paths exist today: OpenSpec tasks (planned, with dependencies and touches) and
agent-filed tickets (unplanned, triaged on arpg with `auto_approve`, held back by
`tickets.py` until the tasks they name are done). Max's work adds review findings, log events
and customer requests, and those arrive continuously, not in planning batches.

## Decisions

- **Signals, then items.** Sources produce signals; the core turns them into tracker items after
  deduplication. Sources never touch the tracker, so a source works with any tracker.
- **Triage is one agent run per sync over new items**, as ticket triage is now, on the
  planner's model, with per-source rules in config injected into the prompt. One batched run
  keeps cost flat as sources grow.
- **OpenSpec stays the planning backbone.** `attach` adds a task to an existing change (so it
  gets planned dependencies and touches); `propose-change` drafts a change for a human,
  because a new change is a scope decision. Plain `approve` items run like today's tickets.
- **Fingerprints are per source**: log groups by exception type and top frame; tracker-query
  by item id; inbox by file hash; agent tickets and review findings by normalized title.
- **Log access stays local or file-based** in the built-ins (a file, a folder, a JSON-lines
  feed someone else exports). Vendor log platforms (Sentry, Application Insights, Datadog) are
  user adapters built with the integration kit.

## Risks / Trade-offs

- [A noisy log floods the tracker] → grouping, a per-source rate limit on new items per sync,
  and a minimum occurrence count before a group becomes a signal.
- [Customer text contains personal data that ends up in prompts and a tracker] → per-source
  `redact` patterns applied before anything is stored; customer sources off by default.
- [Auto-attached tasks change an OpenSpec change's scope unnoticed] → `attach` writes a commit
  that names the source and item, and the change's milestone release notes list attached tasks.

## Open Questions

- Where do customer requests and production logs live at Max's work (Jira service desk,
  e-mail, a monitoring tool)? Decides which sources to build first.
