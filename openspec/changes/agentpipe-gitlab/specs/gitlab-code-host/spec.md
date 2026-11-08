## ADDED Requirements

### Requirement: Merge requests
The `gitlab` CodeHost SHALL create merge requests with a pipeline label, list the open ones
it made with head sha, source and target branch, body and changed files, comment on them, and
merge one with `sha` set to the expected head and squash on, failing as a moved head when GitLab
reports the sha mismatch.

#### Scenario: Moved head
- **WHEN** the dispatcher merges `!42` at sha A and someone pushed B
- **THEN** GitLab refuses and the core treats it as a moved head

### Requirement: Approvals from a reviewer identity
With `approval = "host"`, an agent approval SHALL be posted as a GitLab approval using the
reviewer identity's token, and the merge SHALL wait until GitLab reports the merge request
approved and mergeable. A rejection SHALL revoke that identity's approval if one exists.

#### Scenario: Approval rules need two approvals
- **WHEN** the project requires two approvals and only the reviewer bot approved
- **THEN** the item waits in `awaiting human review` until a second approval arrives, then merges

### Requirement: Releases
For a pushed tag the CodeHost SHALL create a GitLab release with notes and attach assets via
the generic package registry and release asset links.

#### Scenario: Milestone release
- **WHEN** a milestone with two asset files is released
- **THEN** the release page lists both as downloadable links
