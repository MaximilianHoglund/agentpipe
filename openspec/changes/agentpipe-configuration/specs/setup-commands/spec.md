## ADDED Requirements

### Requirement: init writes starter config from answers
`agentpipe init` SHALL ask which tracker, code host, remote or local git, stack, gate and
approval mode to use, write the user file if missing and a `.agentpipe.toml` in the repo, and
print what to run next. It SHALL also run non-interactively from flags.

#### Scenario: Local-only start
- **WHEN** a user answers tracker `none`, git `local`, stack `generic`, test `npm test`
- **THEN** a valid `.agentpipe.toml` is written and `agentpipe doctor` passes on it

### Requirement: doctor checks a target end to end
`agentpipe doctor <target>` SHALL check binaries, credentials, each adapter's connectivity
(a read-only call), the stack's preflight commands from the dispatcher's shell and from an
agent's shell tools, the gate on the base branch when asked, and that agents' deny rules cover
the chosen host's CLI. Each failure SHALL print what to fix.

#### Scenario: Missing token
- **WHEN** the GitLab token variable is unset
- **THEN** doctor reports "code_host gitlab: GITLAB_TOKEN is not set" and exits non-zero

### Requirement: Old config migrates
`agentpipe config migrate` SHALL turn a `pipeline.toml` into a user file plus one
`.agentpipe.toml` per target repo, and the dispatcher SHALL keep reading `pipeline.toml` until
then.

#### Scenario: Max's file
- **WHEN** migrate runs on Max's current `pipeline.toml`
- **THEN** `config show` for arpg, dota and riftbound prints the same effective values as before
