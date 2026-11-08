## ADDED Requirements

### Requirement: Statuses as scoped labels
The `gitlab` Tracker SHALL represent each status as a scoped label `status::<name>` (so GitLab
itself keeps one at a time) and each kind as `kind::<name>`, creating missing labels at start.

#### Scenario: Status change
- **WHEN** the core moves `#12` from pending review to approved
- **THEN** `#12` carries `status::approved` and no other `status::` label

### Requirement: Blocking links when available
The Tracker SHALL use "is blocked by" issue links when the project supports them and declare the
`links` capability only after checking. Without them it SHALL declare no `links`, so the core
fallback applies.

#### Scenario: Premium project
- **WHEN** the project supports blocking links
- **THEN** a blocked-by edge appears in GitLab's issue sidebar as "is blocked by #3"

### Requirement: Human comments are told apart by author
Comments SHALL carry the author's username, and comments by the configured pipeline identities
SHALL be treated as pipeline comments, everything else as human input.

#### Scenario: Teammate comments
- **WHEN** a colleague comments on an issue in `needs decision`
- **THEN** the next agent run gets that comment as authoritative input
