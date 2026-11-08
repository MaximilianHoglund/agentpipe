## ADDED Requirements

### Requirement: Gate on the host's pipeline
The `gitlab-ci` Gate SHALL find the pipeline for the change request's head sha, wait for it to
finish (up to the gate timeout), and report pass when it succeeded, fail with the failed jobs'
names and log tails when it failed, and fail as a timeout otherwise.

#### Scenario: Failed job
- **WHEN** the `test` job fails on the merge request's head
- **THEN** the change request is rejected without a reviewer run, and the rejection names the job and quotes its log tail

### Requirement: Merged-result pipelines count for the merge gate
When the project runs merged-result pipelines, the merge step SHALL use that result instead of
gating locally, and SHALL merge with merge-when-pipeline-succeeds.

#### Scenario: Merge without a local gate
- **WHEN** an approved merge request's merged-result pipeline succeeds
- **THEN** it merges with no local test run
