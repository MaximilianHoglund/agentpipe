## ADDED Requirements

### Requirement: Statuses through workflow transitions
The `jira` Tracker SHALL set a status by finding an available transition to the mapped
workflow status and executing it. When an internal status maps to `label:<name>` plus a base
workflow status, it SHALL move to the base status and set the label, removing other pipeline
status labels.

#### Scenario: Mapped workflow status
- **WHEN** the map says `in progress = "In Progress"` and the core moves `PROJ-12` to in progress
- **THEN** `PROJ-12` is transitioned to In Progress

#### Scenario: Status the workflow lacks
- **WHEN** the map says `needs decision = { status = "To Do", label = "agentpipe-needs-decision" }`
- **THEN** `PROJ-12` is in To Do with that label, and reading it back yields needs decision

#### Scenario: No transition available
- **WHEN** no transition leads from the current state to the mapped one
- **THEN** the Tracker raises a mapping error naming both states, and the item goes to needs decision with that reason

### Requirement: Doctor validates the map
`agentpipe doctor` SHALL read the project's workflow and report every internal status whose
mapped state doesn't exist or can't be reached from the states the pipeline moves it from.

#### Scenario: Missing state
- **WHEN** the map names "Code Review" and the workflow has no such state
- **THEN** doctor fails naming the status and listing the workflow's states

### Requirement: Links, comments and references
The Tracker SHALL use the "Blocks" link type for blocked-by edges (declaring `links`), write
comments as ADF on Cloud and plain text on Data Center, read comment authors by account id, and
render references as the issue key.

#### Scenario: Blocked-by in Jira's UI
- **WHEN** `PROJ-14` is blocked by `PROJ-12`
- **THEN** Jira shows "PROJ-14 is blocked by PROJ-12"

### Requirement: Branches carry the key
Branches and change-request titles for Jira items SHALL start with the issue key
(`PROJ-12-short-slug`), so host integrations link them to the issue.

#### Scenario: GitLab shows the link
- **WHEN** GitLab's Jira integration is on and the dispatcher opens a merge request for `PROJ-12`
- **THEN** the merge request appears in PROJ-12's development panel
