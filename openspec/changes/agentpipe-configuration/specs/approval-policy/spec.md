## ADDED Requirements

### Requirement: Approval mode per target
A target SHALL set `approval` to one of: `agent` (the reviewer agent's approval is enough; the
dispatcher merges), `host` (the reviewer's approval is recorded on the code host as a real
approval, and the dispatcher merges once the host's approval rules are met), or `human` (the
change request is assigned to the configured reviewers and merged only after a human approves).

#### Scenario: Agent approval on GitLab
- **WHEN** `approval = "host"` on GitLab and the reviewer agent approves
- **THEN** the merge request gets an approval from the reviewer identity, and the dispatcher merges it when GitLab reports it mergeable

#### Scenario: Human approval required
- **WHEN** `approval = "human"` and the reviewer agent approves
- **THEN** the item moves to `awaiting human review`, the configured humans are requested as reviewers, and nothing merges until one approves

#### Scenario: Human requests changes
- **WHEN** a human leaves change requests on a merge request in `awaiting human review`
- **THEN** the item goes to `changes requested` and the implementer revises with the human's comments as authoritative input

### Requirement: Separate identities
The dispatcher, implementer-side pushes and the reviewer SHALL each use a configurable identity
(account, token or credential source). When the reviewer and the author are the same identity
and the host forbids self-approval, `approval = "host"` SHALL fail at load with that reason.

#### Scenario: Reviewer bot
- **WHEN** the reviewer identity is a GitLab project access token and the author is Max's user
- **THEN** the reviewer's approval counts toward the merge request's approval rules
