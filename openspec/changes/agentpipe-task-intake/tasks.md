## 1. Port and plumbing

- [ ] 1.1 Add the IntakeSource port, the signal format, per-source state across restarts, and list-valued `task_source`
- [ ] 1.2 Move agent-filed tickets onto an `agent-tickets` source with no change in behaviour

## 2. Built-in sources

- [ ] 2.1 Add `review-findings`, with a follow-up flag on non-blocking findings in the reviewer's output
- [ ] 2.2 Add `tracker-query` for existing items matching a label or query
- [ ] 2.3 Add `log-events` for a log file or JSON-lines feed, with grouping, counts, minimum occurrences and a rate limit
- [ ] 2.4 Add `inbox` for Markdown files in a folder, with a processed folder
- [ ] 2.5 Add per-source `redact` patterns applied before storage

## 3. Triage

- [ ] 3.1 Add fingerprint and judged deduplication, folding duplicates into comments with occurrence counts
- [ ] 3.2 Generalise ticket triage into the four outcomes with per-source rules in the prompt
- [ ] 3.3 Implement `attach` (append a task to a change's tasks.md on the base, then plan it) and `propose-change` (draft a change and wait for acceptance)
- [ ] 3.4 Add priorities and order ready work by milestone, priority, then age
