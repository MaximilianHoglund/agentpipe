## ADDED Requirements

### Requirement: Duplicates are folded into existing items
A signal whose fingerprint matches an open item, or that triage judges a duplicate of one, SHALL
become a comment on that item (with an occurrence count for repeats) instead of a new item.

#### Scenario: Same bug from a log and a customer
- **WHEN** a customer request describes the crash a log-events item already tracks
- **THEN** triage links it as a duplicate and comments on the existing item with the customer's text

### Requirement: Triage outcomes
Each new item SHALL get exactly one triage outcome: `approve` (ready for implementation),
`ask` (waits for a human, with the question as a comment), `attach` (added as a task to a named
OpenSpec change's `tasks.md` on the base branch), or `propose-change` (a new OpenSpec change is
drafted and waits for a human to accept it). Per-source rules SHALL limit outcomes, for example
customer requests never auto-approve.

#### Scenario: Customer request
- **WHEN** a customer request is triaged and its source rule forbids `approve`
- **THEN** its outcome is `ask` or `propose-change`, never `approve`

#### Scenario: Fits an existing change
- **WHEN** a log-events bug lies in the area of an active OpenSpec change
- **THEN** triage may `attach` it, and the task appears in that change's `tasks.md` and is planned like any other

### Requirement: Priority orders ready work
Every item SHALL carry a priority (from source severity, then triage). The scheduler SHALL
order ready work by milestone, then priority, then age.

#### Scenario: Urgent production error
- **WHEN** a high-severity log-events item and an older normal-priority task are both ready and nothing else separates them
- **THEN** the log-events item is dispatched first
