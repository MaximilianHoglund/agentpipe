## 1. Layers

- [ ] 1.1 Load the user file, repo `.agentpipe.toml`, machine overrides and target table with key-by-key precedence and provenance tracking
- [ ] 1.2 Validate against adapter-declared option schemas: unknown keys with suggestions, types, required credentials; refuse literal secrets in the repo file
- [ ] 1.3 Add `agentpipe config show <target>` with each value's source

## 2. Composition

- [ ] 2.1 Read per-port adapter choices and option tables, with documented defaults (`tracker = "none"`, `code_host = "local"`, `approval = "agent"`)
- [ ] 2.2 Reject combinations ruled out by adapter capabilities, naming both settings and a fix
- [ ] 2.3 Add a `none` Tracker that keeps statuses in dispatcher state and shows them in the watcher

## 3. Approval and identities

- [ ] 3.1 Add the `approval` setting with `agent`, `host` and `human` modes in the MergePolicy
- [ ] 3.2 Add the `awaiting human review` status, reviewer assignment, and the revise loop on a human's requested changes
- [ ] 3.3 Add per-role identities (dispatcher, pusher, reviewer) to the CodeHost options; fail at load when `host` approval would be a self-approval

## 4. Setup commands

- [ ] 4.1 Add `agentpipe init`, interactive and flag-driven, writing the user file and `.agentpipe.toml`
- [ ] 4.2 Add `agentpipe doctor`, moving preflight into it and adding credential, connectivity and deny-rule checks
- [ ] 4.3 Add `agentpipe config migrate` and verify Max's arpg, dota and riftbound show identical effective values afterwards
- [ ] 4.4 Rewrite the README for someone who is not Max: install, init, doctor, run
