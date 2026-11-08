## Context

`config.py` builds one `Config` from one `pipeline.toml` with machine overrides merged in.
Options are GitHub- and cargo-flavoured (`github_repo`, `gh_bin`, `clean_workspace_crates`).
After `agentpipe-framework-core`, adapters declare their own options, so the config layer only
has to compose and validate.

## Goals / Non-Goals

**Goals:**
- A newcomer gets a working target from `init` plus `doctor` without reading the code.
- Max's three targets keep identical effective settings after migration.
- Approval behaviour that fits both Max (agents approve) and stricter teams (humans approve).

**Non-Goals:**
- A GUI or web config editor.
- Secret storage of its own: environment variables and the OS keyring only.

## Decisions

- **TOML everywhere**, validated by adapter-declared schemas (pydantic or attrs-based). TOML is
  what the project already uses and what Python reads without dependencies.
- **Repo file is checked in, user file is not.** Team-shared facts (test command, stack,
  context docs) belong with the code; accounts, paths and limits belong to the person.
- **`approval` is a MergePolicy concern, identities are a CodeHost concern.** `agent` keeps
  today's comment-based verdicts; `host` needs a second identity because GitHub and GitLab both
  block self-approval by default; `human` adds one internal status, `awaiting human review`,
  and re-enters the existing revise loop on a human's change requests.
- **`doctor` replaces preflight** but runs the same checks at dispatcher start and each sync
  while failing, so behaviour stays.
- **Defaults for the optional stages**: `tracker = "none"`, `code_host = "local"`,
  `approval = "agent"`. The smallest setup needs only git and Claude Code.

## Risks / Trade-offs

- [Three layers are harder to reason about] → `config show` with provenance, and `doctor`
  prints the merged target at the top.
- [A new status for human review needs a label/state on every tracker] → it is part of the
  internal status set from the start, so adapters map it like the others.

## Open Questions

- Whether merge batches (one gate for several approved change requests) can coexist with
  `approval = "human"`, where humans approve at different times. Likely: batch only what is
  approved when the batch forms.
