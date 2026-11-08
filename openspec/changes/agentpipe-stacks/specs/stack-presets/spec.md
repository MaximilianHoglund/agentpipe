## ADDED Requirements

### Requirement: Generic stack
The `generic` Stack SHALL supply no commands, and loading SHALL fail if the target sets no
`test` command, saying the generic stack needs one.

#### Scenario: Missing gate
- **WHEN** a target sets `stack = "generic"` without `test`
- **THEN** loading fails with that message

### Requirement: Shared mutation evidence
Every stack's mutation step SHALL produce a list of records `{file, line, mutation, status}`
where status is `survived`, `timeout` or `no-coverage`, and the reviewer prompt SHALL read only
that format. The rust stack SHALL convert cargo-mutants output to it.

#### Scenario: Rust unchanged in substance
- **WHEN** cargo-mutants reports `MISSED` mutants for arpg
- **THEN** the reviewer sees the same mutants as `survived` records

### Requirement: node and python presets
The `node` and `python` Stacks SHALL provide test, format and preflight defaults (`npm test`,
`npx prettier --write`; `pytest`, `ruff format`) and pass the Stack contract suite.

#### Scenario: Python repo
- **WHEN** a target sets `stack = "python"`
- **THEN** the gate is `pytest` and preflight checks the interpreter the repo's tooling uses
