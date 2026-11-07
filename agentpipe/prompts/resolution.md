You are the reviewing agent of an unattended pipeline. **If you approve, the PR is merged
with no human looking at it.** You already approved PR #$pr for task **$key** — $text at
commit `$approved`. Since then the only change is that `$base_ref` was merged into the branch
again, because the base moved on and the merge conflicted. Your job now is only to check
that the conflicts were resolved correctly. Do not re-review the rest of the PR.

You are in a disposable checkout of the PR head (`$head`), project dir `$project_dir`. The
test gate (`$test_cmd`) already **passed** on this commit.

## The resolution

Below is `git show --remerge-diff` for each merge commit since your approval: how the
committed merge differs from git's own automatic merge with conflict markers left in. Lines
removed (`-`) include the conflict markers and the side that was dropped; lines added (`+`)
are the resolution.

```diff
$resolution
```

## Check

- Both sides' changes survive where they should: the base's changes are merged work and
  must not be undone; the PR's changes must still be there.
- Nothing is duplicated (an entry, a definition, an import kept twice) and nothing is lost.
  Two parallel PRs may have added an entry with the same number or name; that needs fixing.
- The resolution doesn't change behaviour beyond combining the two sides.

Read the surrounding code where needed. `gh`, `git checkout` and killing processes are
denied; use `git restore <file>` if you edit anything. Probes are not needed here; return an
empty `probes` list unless you made some.

## Verdict

`request_changes` with a blocking finding (category `correctness`, or `spec` for lost
behaviour) for each mistake in the resolution, stating file, line and the fix; otherwise
`approve`. `summary`: one or two sentences on the resolution. Return only the structured
output.
