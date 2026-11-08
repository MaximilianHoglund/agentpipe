## Context

The dispatcher polls its tracker and code host every `poll_seconds` and treats human edits as
input. A file-based tracker fits that model directly: polling reads files instead of an API.

## Goals / Non-Goals

**Goals:**
- The full loop with no network services other than Claude.
- Items readable and editable by hand, and committable to the repo if the user wants.
- A clean path to a hosted tracker later.

**Non-Goals:**
- Multi-machine sharing of local items (a user who wants that commits the items directory or
  uses a hosted tracker).
- A web UI for local items; the watcher and the CLI are the interface.

## Decisions

- **Markdown with front matter**, one file per item: diffable, greppable, editable in any
  editor or Obsidian. Alternative: SQLite, rejected because humans can't edit it by hand.
- **Atomic writes** (write to a temp file in the same directory, then rename) and a per-target
  lock file, so the dispatcher and a human's `agentpipe item` command can't interleave.
- **Change requests in the work dir, branches in the user's repo.** The record holds verdicts
  and comments that would otherwise be review comments on a host.
- **Comment authorship** comes from the writer (`agent:reviewer`, `human:<git user.name>`),
  which makes "human comments are authoritative" trivial here.

## Risks / Trade-offs

- [A human edits a file while the dispatcher writes it] → lock file plus re-read on conflict;
  the human's edit wins when timestamps tie.
- [Local merges into the branch the user has checked out] → merge in the dispatcher's own base
  worktree and update the branch ref; warn if the user's checkout has uncommitted changes on it.
