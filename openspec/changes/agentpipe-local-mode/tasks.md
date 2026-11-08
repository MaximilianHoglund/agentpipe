## 1. Local tracker

- [ ] 1.1 Add the item file format (front matter plus body and comments) with atomic writes and a per-target lock
- [ ] 1.2 Implement the `local` Tracker: list, create, status, comment, close, links and reverse links, passing the Tracker contract tests
- [ ] 1.3 Add `agentpipe item list|show|comment|status|approve`
- [ ] 1.4 Let the watcher answer a `needs decision` item: write a comment and set the status from the pane view

## 2. Local code host

- [ ] 2.1 Implement the `local` CodeHost: change-request records, changed files from git, comments and verdicts, passing the CodeHost contract tests
- [ ] 2.2 Implement local squash merge at an expected head in the dispatcher's base worktree, with optional push
- [ ] 2.3 Implement local releases: annotated tag plus an assets folder

## 3. End to end and export

- [ ] 3.1 Add an end-to-end test: a throwaway repo with no remote, two OpenSpec tasks, one blocked by the other, run to both merged with stubbed agents
- [ ] 3.2 Add `agentpipe export --to <tracker>` with a mapping file and the target switch-over
