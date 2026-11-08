## 1. Client

- [ ] 1.1 Add the GitLab REST client: base URL, per-identity tokens from env or keyring, pagination, 429 backoff, injectable transport for recording
- [ ] 1.2 Set up a gitlab.com sandbox project and record fixtures for the contract suites

## 2. Code host

- [ ] 2.1 Implement merge requests: create with label, list open with changed files, comment, merge at sha with squash
- [ ] 2.2 Implement host approvals from the reviewer identity, approval revocation on rejection, and waiting for mergeability
- [ ] 2.3 Implement releases with asset uploads to the generic package registry and release links
- [ ] 2.4 Pass the CodeHost contract suite offline and live against the sandbox

## 3. Tracker

- [ ] 3.1 Implement issues with scoped `status::` and `kind::` labels, comments with authors and close reasons
- [ ] 3.2 Implement blocking links with a capability probe and the no-links fallback
- [ ] 3.3 Pass the Tracker contract suite offline and live against the sandbox

## 4. CI gate and checks

- [ ] 4.1 Implement the `gitlab-ci` Gate: find the pipeline by sha, wait, report failed jobs with log tails
- [ ] 4.2 Use merged-result pipelines for the merge step when the project runs them
- [ ] 4.3 Teach `doctor` to read the project's merge settings and approval rules and report which approval modes will work
- [ ] 4.4 Add `glab` to the default deny rules
