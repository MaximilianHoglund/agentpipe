## ADDED Requirements

### Requirement: Sources yield signals
An IntakeSource SHALL yield signals with title, body, origin (source name and link), a
fingerprint for deduplication, and an optional severity, and SHALL remember what it has already
yielded across restarts. Sources SHALL be configured per target as a list, each with options.

#### Scenario: Two sources on one target
- **WHEN** a target configures `agent-tickets` and `log-events`
- **THEN** each sync polls both, and each signal names which source it came from

### Requirement: Built-in sources
agentpipe SHALL ship `agent-tickets`, `review-findings`, `tracker-query`, `log-events` and
`inbox` sources. `log-events` SHALL read a log file or JSON-lines feed, group events by a
configurable fingerprint (default: error type plus top stack frame), and yield one signal per
new group. `inbox` SHALL turn each Markdown file in a folder into a signal and move it to a
processed folder.

#### Scenario: Repeated error
- **WHEN** the same exception appears 30 times in the log since the last sync
- **THEN** one signal is yielded for its group, carrying the count and the latest sample

#### Scenario: Customer request dropped as a file
- **WHEN** `inbox/export-to-csv.md` appears
- **THEN** a signal is yielded with its contents and the file moves to `inbox/processed/`

### Requirement: Review findings become follow-ups
`review-findings` SHALL yield a signal for each non-blocking reviewer finding the reviewer marks
as worth a follow-up, linked to the change request it came from.

#### Scenario: Non-blocking finding
- **WHEN** a reviewer approves with a non-blocking `improvement` finding flagged follow-up
- **THEN** an intake signal links to that review
