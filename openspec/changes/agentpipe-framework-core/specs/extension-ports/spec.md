## ADDED Requirements

### Requirement: The core names no vendor, tool or stack
The scheduler, state, planning, review-verdict rules and conflict handling SHALL reach external
systems only through the ports Tracker, CodeHost, TaskSource, Stack, Gate, MergePolicy and
AgentRuntime. Core modules MUST NOT import an adapter module or name a vendor, CLI or build tool.

#### Scenario: A lint guards the boundary
- **WHEN** a core module imports `agentpipe.adapters.*` or contains `gh `, `cargo`, `gitlab` or `jira` outside a string supplied by config
- **THEN** the boundary test fails and names the module and line

### Requirement: Tracker port
A Tracker SHALL list the pipeline's work items with their status, create an item, set its
status, comment on it, close it (completed or not planned), read its comments with author and
time, and add, remove and list blocked-by links. It SHALL declare its capabilities, at least
`links` (native blocked-by) and `labels`, and render an item reference in its own form
(`#12`, `PROJ-12`).

#### Scenario: A tracker without native links
- **WHEN** a Tracker declares no `links` capability
- **THEN** the core keeps blocked-by edges in its own state, enforces them in scheduling, and writes them into the item body as text

#### Scenario: References render per tracker
- **WHEN** the core writes a comment that refers to another item
- **THEN** the reference is produced by the Tracker, never formatted by the core

### Requirement: CodeHost port
A CodeHost SHALL list open change requests the pipeline made, with head sha, source and target
branch, changed files and body; create one; comment on one; merge one at an expected head sha
(failing if the head moved); and publish a release for a pushed tag with assets. Change-request
ids and work-item ids SHALL be separate types, so no call assumes they share a number space.

#### Scenario: GitLab-style separate numbering
- **WHEN** work item 5 and change request 5 both exist
- **THEN** a comment meant for the change request reaches the change request, not the item

#### Scenario: Merge at a moved head
- **WHEN** the core merges a change request at sha A and its head is now B
- **THEN** the CodeHost refuses the merge and the core treats it as a moved head

### Requirement: TaskSource port
A TaskSource SHALL list planned tasks with a stable key, title, body, group and text hash; mark a
task done on the base branch after its change request merges; report when a group is complete;
and run an optional completion step for a group (OpenSpec: archive).

#### Scenario: OpenSpec keeps today's behaviour
- **WHEN** the `openspec` TaskSource is selected
- **THEN** tasks come from `openspec/changes/<change>/tasks.md`, merges tick them, and a completed change is archived with the configured command

### Requirement: Stack port
A Stack SHALL supply default commands (test, format, mutation, preflight), per-slot environment
for build isolation, a pre-run cleanup step, default overlap-ignore globs, and prompt notes for
agents. Every value SHALL be overridable by target config. Stacks SHALL compose, so a target can
use more than one (e.g. `rust` and `godot`).

#### Scenario: Rust isolation moves into the stack
- **WHEN** a target uses the `rust` stack
- **THEN** each slot gets its own `CARGO_TARGET_DIR` and `clean_workspace_crates` behaves as before, and no core module mentions cargo

### Requirement: Gate port
A Gate SHALL run the test gate for a checkout at a commit and return pass or fail with a log
excerpt and duration. The built-in `local-command` Gate SHALL keep today's behaviour.

#### Scenario: Gate failure is a rejection
- **WHEN** the Gate reports failure on a change request's head
- **THEN** the change request is rejected without an agent run, as today

### Requirement: MergePolicy port
A MergePolicy SHALL decide what follows an agent approval: merge it, or hand it to a human and
wait. The built-in `auto-merge` policy SHALL keep today's behaviour, including merge batches.

#### Scenario: Auto-merge unchanged
- **WHEN** the reviewer approves and the policy is `auto-merge`
- **THEN** the change request is merged exactly as today

### Requirement: Behaviour is unchanged for existing targets
After this change, Max's targets (arpg, dota, riftbound) SHALL run with the same results: every
existing test passes against port fakes, and a dry-run sync of each target produces the same
actions as before the change.

#### Scenario: Dry-run parity
- **WHEN** `agentpipe sync <target> --dry-run` runs on the old and the new code against the same repo state
- **THEN** both print the same actions
