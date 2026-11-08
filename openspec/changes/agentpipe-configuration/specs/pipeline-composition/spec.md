## ADDED Requirements

### Requirement: Each stage is chosen per target
A target SHALL name an adapter for each port: `tracker`, `code_host`, `task_source`, `stack`
(one or a list), `gate`, `merge_policy` and `runtime`. Each adapter's options SHALL live in a
table named after the port (`[targets.x.tracker]`). Omitted stages take documented defaults.

#### Scenario: Max's work target
- **WHEN** a target sets `tracker = "jira"`, `code_host = "gitlab"`, `task_source = ["openspec", "intake"]`, `stack = "dotnet"`
- **THEN** the dispatcher plans OpenSpec and intake work into Jira, opens merge requests on GitLab, and gates with the dotnet stack's test command

### Requirement: Optional stages
The configuration SHALL accept `tracker = "none"` (statuses live only in the dispatcher's state and the watcher) or
`tracker = "local"` (items stored as files), and `code_host = "local"` (no remote: change requests are
local branches with review records in the work dir). The core SHALL work end to end with
`tracker = "local"` and `code_host = "local"`.

#### Scenario: A user with no tracker and no remote
- **WHEN** a target sets `tracker = "none"` and `code_host = "local"`
- **THEN** tasks are implemented on local branches, reviewed, merged into the local base branch, and the watcher shows every status

### Requirement: Impossible combinations are rejected
Loading SHALL reject combinations an adapter's capabilities rule out, naming both settings,
such as `releases` with a code host lacking releases, or `approval = "host"` with a code host
lacking approvals.

#### Scenario: Release without a host that publishes releases
- **WHEN** a target with `code_host = "local"` defines milestones with release assets
- **THEN** loading fails saying local hosts tag releases but cannot publish assets, and suggests `release_assets_dir`
