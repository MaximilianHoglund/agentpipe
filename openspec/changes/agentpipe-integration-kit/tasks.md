## 1. Contract suites

- [ ] 1.1 Write the Tracker contract suite, including the links capability and the core fallback
- [ ] 1.2 Write the CodeHost contract suite, including moved-head merges, separate id spaces and releases
- [ ] 1.3 Write the TaskSource, Stack, Gate and MergePolicy contract suites
- [ ] 1.4 Run the built-in adapters (github, openspec, rust, godot, local-command, auto-merge) through their suites in CI
- [ ] 1.5 Add `agentpipe adapter test` with offline, `--live <target>` and `--record` modes, run-id tagging and cleanup

## 2. Record and replay

- [ ] 2.1 Add transport recording for subprocess and HTTP calls, with secret scrubbing that fails on any remaining match
- [ ] 2.2 Record GitHub fixtures from a sandbox repo so the github adapter's suite runs offline

## 3. Scaffolding and docs

- [ ] 3.1 Add `agentpipe adapter new <kind> <name>` and `agentpipe adapter list`
- [ ] 3.2 Generate the port reference from the protocols and write the authoring guide with the github adapter as the worked example

## 4. Guided setup

- [ ] 4.1 Write the setup guide prompt covering every stage, built-in choices and their trade-offs, and the no-credentials-in-files rule
- [ ] 4.2 Add `agentpipe setup`, launching the session with the right tool permissions and a setup notes file for resuming
- [ ] 4.3 Dry-run the setup with a person who isn't Max on a fresh machine and fix what they get stuck on
