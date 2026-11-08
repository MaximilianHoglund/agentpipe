## Why

Max's workplace uses GitLab.com with Premium features (blocking issue links work). For agentpipe
to run there, GitLab must serve as a code host (merge requests, approvals, merges, releases)
and optionally as a tracker (issues with labels and blocking links). It is also the first
built-in adapter beyond GitHub, so it proves the ports.

## What Changes

- A `gitlab` CodeHost over the GitLab REST API: merge requests (create, list, comment, changed
  files, merge at sha with squash), approvals from a reviewer identity, merge-when-pipeline-
  succeeds, releases with asset links, and GitLab CI pipeline status for the gate.
- A `gitlab` Tracker: issues with scoped labels (`status::…`, `kind::…`), comments, and
  "is blocked by" links (Premium), with a capability check that falls back to the core-owned
  edges when links are unavailable.
- A `gitlab-ci` Gate: wait for the merge request's pipeline on its head sha and use its result,
  as an alternative to the local gate.
- Separate number spaces handled throughout (`#iid` for issues, `!iid` for merge requests).
- Token-based auth (project or personal access token from env or keyring), self-managed base
  URL supported.

## Capabilities

### New Capabilities
- `gitlab-code-host`: merge requests, approvals, merges, releases on GitLab.
- `gitlab-tracker`: GitLab issues as the tracker, with scoped-label statuses and blocking links.
- `remote-ci-gate`: using a host's CI pipeline result as the test gate.

### Modified Capabilities

## Impact

- New `agentpipe/adapters/gitlab/` (httpx client, no `glab` dependency); deny rules gain
  `glab`; fixtures recorded from a gitlab.com sandbox project.
- Depends on `agentpipe-framework-core`, `agentpipe-configuration` (approval modes, identities)
  and `agentpipe-integration-kit` (contract suites).
