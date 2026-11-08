## Why

At work, tasks come from an OpenSpec plan but also arrive continuously: findings from reviews,
error events in logs, customer requests, and tickets agents file themselves. Today only OpenSpec
tasks and agent-filed tickets enter the pipeline, each by its own code path. A general intake
lets any source feed work in, with triage deciding what is ready, what needs a human and what
belongs in an OpenSpec change.

## What Changes

- An `IntakeSource` port: a source yields raw signals (title, body, origin, link, fingerprint,
  severity). Built-ins: `agent-tickets` (today's defect and improvement filing), `review-findings`
  (non-blocking reviewer findings worth a follow-up), `tracker-query` (existing items matching a
  label or query, e.g. customer requests a support team files), `log-events` (an error-log file
  or a JSON-lines feed), and `inbox` (Markdown files dropped in a folder).
- Deduplication: signals with a known fingerprint, or judged duplicates of an open item, become
  comments on that item instead of new items; repeated log events raise its occurrence count.
- Triage generalised from today's ticket triage: each new item is approved, sent to a human
  with a question, attached to an existing OpenSpec change as a task, or proposed as a new
  OpenSpec change for a human to accept. Per-source rules set what may be auto-approved.
- Prioritisation: items carry a priority from source severity and triage, and the scheduler
  orders ready work by milestone, then priority, then age.
- `task_source` takes a list, so OpenSpec and intake run side by side on one target.

## Capabilities

### New Capabilities
- `intake-sources`: the source port, the built-in sources and the signal format.
- `intake-triage`: deduplication, triage outcomes, routing into OpenSpec, and priority.

### Modified Capabilities

## Impact

- Replaces the ticket-filing and ticket-triage paths in the core with intake sources and the
  triage step; `prompts/ticket_triage.md` becomes the triage prompt with per-source rules.
- Scheduler ordering gains priority. Depends on `agentpipe-framework-core` and
  `agentpipe-configuration`; works with any tracker, including `local` and `none`.
