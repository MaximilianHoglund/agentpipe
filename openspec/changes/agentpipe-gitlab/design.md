## Context

GitLab differs from GitHub in ways the ports already anticipate: separate `#` and `!` id spaces,
scoped labels, approvals as a first-class feature, blocking links on Premium, and CI as the
usual source of truth for tests. Max's work instance is gitlab.com with Premium features.

## Decisions

- **REST via httpx, not `glab`.** No extra CLI for users to install and keep logged in; tokens
  come from env or keyring per identity; the transport is injectable for recording.
- **Scoped labels for status.** GitLab enforces one `status::` label at a time, removing a class
  of double-label bugs the GitHub adapter handles by hand.
- **Approval through a second identity** (project access token with Developer role or a bot
  user). GitLab blocks author self-approval by default, so `approval = "host"` needs it;
  `approval = "agent"` works with one identity, posting verdicts as notes like today.
- **CI gate optional**: local gating stays the default because it works without CI minutes and
  keeps review turnaround independent of runner queues; `gitlab-ci` suits teams whose CI is
  the agreed gate.

## Risks / Trade-offs

- [API rate limits on gitlab.com] → poll with `updated_after` filters and ETags; back off on 429.
- [Work project rules unknown: protected branches, required pipelines, approval rules] →
  `doctor` reads the project's merge settings and reports which approval modes will work.

## Open Questions

- Which identity can Max use at work for the reviewer (project access tokens need Maintainer)?
- Does the work project require merged-result pipelines or merge trains?
