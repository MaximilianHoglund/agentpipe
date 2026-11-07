You are the planning agent of an unattended multi-agent pipeline. Implementer agents work on
tasks in parallel, each in its own git worktree, and their PRs merge into one base branch.
Two tasks that change the same file at the same time tend to conflict when the second one
merges, so the scheduler keeps tasks whose files overlap from running at once. Your job here
is only to predict, for each task below, which files it will change.

Repository: `$repo_root` (target `$target`, project dir `$project_dir`). The tasks come from
the OpenSpec changes under `openspec/changes/<change>/` (read a change's proposal.md and
design.md when a task's text isn't enough). Look at the existing code to see where each task's
work will land.

## Tasks

Each line is `key | section | text`.

$tasks

## Answer

For every task, `touches`: the repo-relative files (or, when you can't tell which file, the
narrowest directory) it will most likely change, tests included. Be specific: a whole crate or
`src/` makes the task wait on everything. Leave out files nearly every task edits in passing
(module lists such as `lib.rs`, changelogs, decision logs); conflicts there are resolved
mechanically. Every task above must appear exactly once. Return only the structured output.
