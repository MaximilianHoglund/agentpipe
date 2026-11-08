## ADDED Requirements

### Requirement: Change requests are local branches
The `local` CodeHost SHALL represent a change request as a branch in the user's repository
plus a JSON record (id, branch, base, head sha, title, body, comments, verdicts, state) in the
work dir. Changed files SHALL come from `git diff` against the base.

#### Scenario: Open a change request
- **WHEN** an implementer finishes task T-4 on branch `agent/t-4`
- **THEN** a record `CR-4` exists with the branch's head sha, and the reviewer picks it up

### Requirement: Local merge at an expected head
Merging SHALL squash-merge the branch into the local base at the expected head sha, refuse if
the head moved, and delete the branch afterwards. With `push = true` it SHALL also push the base.

#### Scenario: No remote at all
- **WHEN** the repository has no remote and a change request is approved
- **THEN** the base branch gains one squash commit and no network call is made

### Requirement: Releases become tags and a folder
Without a hosted release feature, a release SHALL create an annotated tag and copy its assets to
`<work_dir>/releases/<tag>/`.

#### Scenario: Milestone release locally
- **WHEN** a milestone completes on a `local` code host
- **THEN** the tag exists in the repo and its assets are in the releases folder
