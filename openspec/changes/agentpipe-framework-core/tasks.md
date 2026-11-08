## 1. Own repository

- [x] 1.1 Split `pipeline/` out of the monorepo with its history into a new `agentpipe` repository, and make it installable with `uv tool install git+<url>`
- [x] 1.2 Move this change and the other `agentpipe-*` OpenSpec changes into the new repository's `openspec/`
- [ ] 1.3 Add a schema version to `state.json` and `plan.json`, with an in-place migration from the unversioned files
- [ ] 1.4 Install the package on the desktop and in the laptop's WSL, update `laptop-run.sh`, and record the steps in the README

## 2. Ports and registry

- [ ] 2.1 Add `agentpipe/ports/` with the Tracker, CodeHost, TaskSource, Stack, Gate, MergePolicy and AgentRuntime protocols, `ItemRef`/`ChangeRef` types and capability sets
- [ ] 2.2 Add the adapter registry: decorator registration, entry points, user and repo plugin directories, override logging, contract versions, unknown-name errors
- [ ] 2.3 Add the boundary test that fails when a core module imports an adapter or names a vendor, CLI or build tool

## 3. GitHub behind the ports

- [ ] 3.1 Split `GitHub` into a `github` Tracker and CodeHost adapter with the label status mapping and native links
- [ ] 3.2 Replace every `self.gh` call in `pipeline.py` with Tracker or CodeHost calls using typed refs; render references through the Tracker
- [ ] 3.3 Add the core-owned blocked-by fallback for trackers without `links`, with the edges written into the item body
- [ ] 3.4 Replace `FakeGitHub` in the tests with in-memory port fakes; all existing tests pass
- [ ] 3.5 Split `pipeline.py` into sync, dispatch, implement, review, merge and release modules along the new seams

## 4. Task source, stack, gate, merge policy

- [ ] 4.1 Move `tasks.md` parsing, ticking, change discovery, archive and milestone completion behind the `openspec` TaskSource
- [ ] 4.2 Move slot `CARGO_TARGET_DIR`, `clean_workspace_crates` and checkout-scratch cleanup into a `rust` Stack, and the Godot preflight and notes into a `godot` Stack
- [ ] 4.3 Move the test gate into the `local-command` Gate and post-approval merging (including merge batches) into the `auto-merge` MergePolicy
- [ ] 4.4 Move OpenSpec, stack and platform wording out of prompts into TaskSource and Stack prompt notes; the owner's name and the product's user word come from config

## 5. Agent runtime and accounting

- [ ] 5.1 Wrap `claude.py` as the `claude-code` AgentRuntime with per-machine and per-target account settings
- [ ] 5.2 Add `windows`, `budget` and `none` accounting, with throttling kept per account

## 6. Parity and cut-over

- [ ] 6.1 Add a dry-run parity script that runs sync on two code versions against the same repo and diffs the actions
- [ ] 6.2 Run parity on arpg, dota and riftbound and fix every difference
- [ ] 6.3 Cut the three live targets over to the package and remove the old `pipeline/` folder from the monorepo once they have run for a day
