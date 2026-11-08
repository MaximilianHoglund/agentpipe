## 1. Client and sandbox

- [ ] 1.1 Scaffold `tracker/jira` with the integration kit and add the REST client: Cloud v3 and DC v2, auth from env or keyring, pagination, rate-limit backoff
- [ ] 1.2 Set up a free Jira Cloud sandbox project and record contract-suite fixtures

## 2. Tracker

- [ ] 2.1 Implement list (JQL on the pipeline label), create, comment (ADF and plain text), authors and close by transition with resolution
- [ ] 2.2 Implement the status map: transition search, label-backed statuses, read-back precedence, required transition fields
- [ ] 2.3 Implement "Blocks" links and reverse lookups
- [ ] 2.4 Pass the Tracker contract suite offline and live against the sandbox

## 3. Integration

- [ ] 3.1 Add tracker-supplied branch prefixes so branches and change-request titles start with the issue key
- [ ] 3.2 Teach `doctor` to validate the status map against the project's workflow
- [ ] 3.3 Run a target with `tracker = "jira"` and `code_host = "gitlab"` on the two sandboxes end to end with stubbed agents
