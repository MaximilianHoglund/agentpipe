## ADDED Requirements

### Requirement: Every port has a contract suite
agentpipe SHALL ship a contract test suite per port that exercises every method and every
declared capability, including failure paths (moved head on merge, missing item, duplicate
link, permission error). Built-in adapters SHALL pass their suites in CI.

#### Scenario: A capability is tested only when declared
- **WHEN** a Tracker declares no `links` capability
- **THEN** the link tests are skipped and the core-fallback tests run instead

### Requirement: Suites run against fakes, fixtures or a sandbox
`agentpipe adapter test <kind>/<name>` SHALL run the suite offline against recorded fixtures by
default, and against a live sandbox when given `--live <target>`. Live runs SHALL only touch
items and branches they created, tagged with a run id, and clean them up.

#### Scenario: Live GitLab sandbox
- **WHEN** a user runs `agentpipe adapter test code_host/gitlab --live sandbox`
- **THEN** the suite creates branches and merge requests prefixed `agentpipe-contract-<id>`, checks every method, and deletes what it made

### Requirement: Record and replay
With `--record`, a live run SHALL save each request and response as fixtures with tokens,
emails and hostnames-in-credentials scrubbed. A fixture that still matches a secret pattern
SHALL fail the recording.

#### Scenario: Offline afterwards
- **WHEN** fixtures were recorded for `tracker/linear`
- **THEN** `agentpipe adapter test tracker/linear` passes with the network disabled
