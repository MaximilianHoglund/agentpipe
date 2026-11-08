## ADDED Requirements

### Requirement: agentpipe is its own repository and package
agentpipe SHALL live in its own git repository with its history, install with
`uv tool install git+<url>`, and expose the `agentpipe` command. It SHALL run on Windows,
Linux and WSL.

#### Scenario: Fresh install
- **WHEN** a user on a machine without the monorepo runs `uv tool install git+<url>`
- **THEN** `agentpipe --version` works and `agentpipe targets` reports that no config exists yet and how to create one

### Requirement: Run data stays compatible
Run data (`state.json`, `plan.json`, logs) SHALL carry a schema version. The new code SHALL read
the files the current dispatcher writes and migrate them in place.

#### Scenario: Restart after the move
- **WHEN** the arpg dispatcher is stopped on the old code and started on the new package
- **THEN** it resumes every in-flight task, PR and session from the existing state files
