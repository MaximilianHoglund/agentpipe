## ADDED Requirements

### Requirement: Three layers with fixed precedence
Configuration SHALL be read from the user file, then the repo's `.agentpipe.toml`, then the
active machine's overrides, then the target's own table, with later layers winning key by key.
Secrets SHALL NOT be read from the repo file; it may only name the environment variable or
keyring entry that holds one.

#### Scenario: Machine override
- **WHEN** the repo file sets `test = "dotnet test"` and the laptop's machine section sets `test = "dotnet test -p:Parallel=false"`
- **THEN** on the laptop the gate runs the second command, and on other machines the first

#### Scenario: A token in the repo file
- **WHEN** `.agentpipe.toml` contains a key named `token` with a literal value
- **THEN** loading fails, saying to use `token_env` or `token_keyring` instead

### Requirement: Provenance is visible
`agentpipe config show <target>` SHALL print every effective value with the file and line it
came from, and mark defaults as defaults.

#### Scenario: Where did this come from
- **WHEN** a user runs `agentpipe config show work-api`
- **THEN** each line reads like `gate.timeout = 1800  (~/.config/agentpipe/config.toml:14)`

### Requirement: Validation fails early with a fix
Loading SHALL reject unknown keys (with a closest-match suggestion), wrong types, options an
adapter doesn't declare, and missing required credentials, before any dispatcher work starts.

#### Scenario: Typo
- **WHEN** a target sets `merge_bacth = 4`
- **THEN** loading fails with "unknown key merge_bacth; did you mean merge_batch?"
