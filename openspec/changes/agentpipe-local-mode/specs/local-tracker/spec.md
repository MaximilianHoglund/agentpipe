## ADDED Requirements

### Requirement: Items are files
The `local` Tracker SHALL store each item as one Markdown file with YAML front matter (id,
title, status, kind, blocked_by, created, updated) and a body followed by a comments section,
under a configured directory. Ids SHALL be short and stable (`T-12`). Writes SHALL be atomic.

#### Scenario: Create and read back
- **WHEN** sync creates an item for task `crafting/1.1`
- **THEN** `items/T-1.md` exists with status `pending implementation` and the task text as its body

#### Scenario: Crash mid-write
- **WHEN** the dispatcher is killed while updating an item
- **THEN** the file holds either the old or the new content, never a partial one

### Requirement: Humans edit items directly or by command
A status or comment a human writes into the file SHALL be picked up on the next poll, exactly
as a label change or comment on a hosted tracker is. `agentpipe item comment|status|approve`
SHALL make the same edits.

#### Scenario: Answering a decision
- **WHEN** a human runs `agentpipe item comment T-7 "Use the cheaper option"` and `agentpipe item status T-7 "pending implementation"`
- **THEN** the next implementer run for T-7 gets the comment as authoritative input

### Requirement: Links are native
The `local` Tracker SHALL declare the `links` capability and store blocked-by edges in front
matter, with the reverse ("blocking") computed on read.

#### Scenario: Blocking backlink
- **WHEN** T-3 is blocked by T-1
- **THEN** listing T-1's blocking items returns T-3

### Requirement: Export to a real tracker
`agentpipe export --to <tracker>` SHALL create every local item in the target tracker with its
status, comments (attributed in text) and links, write a mapping file, and switch the target's
`tracker` setting.

#### Scenario: Moving to GitLab
- **WHEN** a user with 40 local items exports to `gitlab`
- **THEN** 40 issues exist with matching statuses and blocking links, and the dispatcher resumes against GitLab
