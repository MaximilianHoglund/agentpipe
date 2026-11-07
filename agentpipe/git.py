"""Git operations on the target repo and the pipeline's worktrees."""

from __future__ import annotations

import shutil
from pathlib import Path

from agentpipe import proc


class Git:
    def __init__(self, repo: Path, run=proc.run):
        self.repo = repo
        self._run = run

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> proc.Result:
        return self._run(["git", *args], cwd or self.repo, check=check)

    def fetch(self) -> None:
        self.git("fetch", "origin", "--prune")

    def worktree_add(self, path: Path, branch: str, start: str) -> None:
        """Create (or reset) ``branch`` at ``start`` and check it out at ``path``."""
        self.worktree_remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "-B", branch, str(path), start)

    def worktree_detached(self, path: Path, ref: str) -> None:
        self.worktree_remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", str(path), ref)

    def worktree_remove(self, path: Path) -> None:
        if path.exists():
            self.git("worktree", "remove", "--force", str(path), check=False)
            if path.exists():  # not a registered worktree, or Windows held a file
                shutil.rmtree(path, ignore_errors=True)
        self.git("worktree", "prune", check=False)

    def remote_branch_exists(self, branch: str) -> bool:
        return self.git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
                        check=False).ok

    def remote_tags(self) -> set[str]:
        out = self.git("ls-remote", "--tags", "origin").out
        return {line.split("refs/tags/", 1)[1].removesuffix("^{}")
                for line in out.splitlines() if "refs/tags/" in line}

    def head(self, wt: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=wt).out.strip()

    def commits_ahead(self, wt: Path, base_ref: str) -> int:
        return int(self.git("rev-list", "--count", f"{base_ref}..HEAD", cwd=wt).out.strip())

    def is_clean(self, wt: Path) -> bool:
        return self.git("status", "--porcelain", cwd=wt).out.strip() == ""

    def commit_all(self, wt: Path, message: str) -> bool:
        """Commit every change in ``wt`` (git-ignored files stay out). False if nothing to commit."""
        if self.is_clean(wt):
            return False
        self.git("add", "-A", cwd=wt)
        self.git("commit", "--quiet", "--no-verify", "-m", message, cwd=wt)
        return True

    def contains(self, wt: Path, ref: str) -> bool:
        """True if ``ref`` is already an ancestor of HEAD."""
        return self.git("merge-base", "--is-ancestor", ref, "HEAD", cwd=wt, check=False).ok

    def enable_rerere(self) -> None:
        """Record conflict resolutions, so a branch that merges its base again isn't asked to
        resolve the same conflict twice. Repo config, so every worktree shares the records."""
        self.git("config", "rerere.enabled", "true")

    def merge(self, wt: Path, ref: str) -> bool:
        """Merge ``ref`` into HEAD. On conflict the merge is left in progress and False returned.

        diff3 markers show the common ancestor, which tells an additive conflict (both sides
        only added lines) from a real one; rerere's autoUpdate stages files it resolved from
        an earlier resolution, so only new conflicts stay unmerged."""
        return self.git("-c", "merge.conflictStyle=diff3", "-c", "rerere.autoUpdate=true",
                        "merge", "--no-edit", ref, cwd=wt, check=False).ok

    def conflicted(self, wt: Path) -> list[str]:
        """Paths still unmerged in an in-progress merge."""
        out = self.git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).out
        return [l.strip() for l in out.splitlines() if l.strip()]

    def merge_commits(self, wt: Path, since: str, base_ref: str) -> tuple[list[str], int]:
        """The branch's own commits after ``since``, leaving out what came in from ``base_ref``:
        (its merge commits, newest first; how many other commits it has)."""
        span = ["HEAD", f"^{since}", f"^{base_ref}"]
        merges = self.git("rev-list", "--merges", *span, cwd=wt).out.split()
        others = self.git("rev-list", "--no-merges", "--count", *span, cwd=wt).out.strip()
        return merges, int(others or 0)

    def remerge_diff(self, wt: Path, commit: str) -> str:
        """How a merge commit differs from git's own automatic merge: its conflict resolutions."""
        return self.git("show", "--remerge-diff", "--format=", commit, cwd=wt).out

    def merge_abort(self, wt: Path) -> None:
        self.git("merge", "--abort", cwd=wt, check=False)

    def merging(self, wt: Path) -> bool:
        return self.git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", cwd=wt, check=False).ok

    def push(self, wt: Path, branch: str) -> None:
        self.git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=wt)

    def delete_remote_branch(self, branch: str) -> None:
        self.git("push", "origin", "--delete", branch, check=False)

    def delete_branch(self, branch: str) -> None:
        self.git("branch", "-D", branch, check=False)

    def diff(self, wt: Path, base_ref: str, stat: bool = False, relative: bool = False) -> str:
        """Diff of HEAD against its merge base with ``base_ref``.

        ``relative`` limits it to ``wt`` (which may be a subdirectory of a worktree)
        and makes paths relative to it, as tools run from a project dir expect.
        """
        args = ["diff", f"{base_ref}...HEAD"] + (["--stat"] if stat else []) + (["--relative"] if relative else [])
        return self.git(*args, cwd=wt).out

    def restore(self, wt: Path) -> None:
        """Discard every change and untracked file, back to HEAD."""
        self.git("reset", "--hard", "--quiet", cwd=wt)
        self.git("clean", "-fdq", cwd=wt)
