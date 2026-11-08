"""Thin wrapper over the gh CLI for the calls the pipeline needs."""

from __future__ import annotations

import json
from pathlib import Path

from agentpipe import proc
from agentpipe.status import KIND_LABELS, LABEL_INFO, LEGACY_PENDING_APPROVAL, PIPELINE_LABEL, Status


class GitHub:
    def __init__(self, repo: str, gh_bin: str = "gh", cwd: Path | None = None, run=proc.run):
        self.repo = repo
        self.gh_bin = gh_bin
        self.cwd = cwd
        self._run = run

    # -- plumbing ---------------------------------------------------------
    def _gh(self, *args: str, input: str | None = None) -> str:
        return self._run([self.gh_bin, *args], self.cwd, input=input).out

    def _json(self, *args: str):
        out = self._gh(*args).strip()
        return json.loads(out) if out else None

    def _api(self, path: str, *args: str):
        return self._json("api", f"repos/{self.repo}/{path}", *args)

    # -- labels -----------------------------------------------------------
    def ensure_labels(self) -> None:
        self._gh("label", "create", PIPELINE_LABEL, "-R", self.repo, "--color", "ededed",
                 "--description", "Managed by agentpipe", "--force")
        try:
            # Renaming keeps the label on its issues; it fails once the old label is gone.
            self._gh("label", "edit", LEGACY_PENDING_APPROVAL, "-R", self.repo,
                     "--name", Status.PENDING_REVIEW.label)
        except proc.CommandError:
            pass
        for status, (color, desc) in LABEL_INFO.items():
            self._gh("label", "create", status.label, "-R", self.repo, "--color", color,
                     "--description", desc, "--force")
        for kind, (color, desc) in KIND_LABELS.items():
            self._gh("label", "create", f"kind: {kind}", "-R", self.repo, "--color", color,
                     "--description", desc, "--force")

    # -- issues -----------------------------------------------------------
    def list_issues(self) -> list[dict]:
        """Every pipeline issue, open and closed: number, state, title, body, labels (names)."""
        items = self._json("issue", "list", "-R", self.repo, "--label", PIPELINE_LABEL,
                           "--state", "all", "--limit", "5000",
                           "--json", "number,state,title,body,labels") or []
        for it in items:
            it["labels"] = [l["name"] for l in it["labels"]]
        return items

    def create_issue(self, title: str, body: str, status: Status, extra_labels: tuple = ()) -> int:
        labels = [a for l in (PIPELINE_LABEL, status.label, *extra_labels) for a in ("--label", l)]
        url = self._gh("issue", "create", "-R", self.repo, "--title", title, "--body-file", "-",
                       *labels, input=body).strip()
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def set_status(self, number: int, new: Status, old: Status | None) -> None:
        args = ["issue", "edit", str(number), "-R", self.repo, "--add-label", new.label]
        for s in Status:
            if s is not new and (old is None or s is old):
                args += ["--remove-label", s.label]
        self._gh(*args)

    def comment(self, number: int, body: str) -> None:
        """Comment on an issue or PR (they share a number space)."""
        self._gh("issue", "comment", str(number), "-R", self.repo, "--body-file", "-", input=body)

    def close_issue(self, number: int) -> None:
        self._gh("issue", "close", str(number), "-R", self.repo, "--reason", "completed")

    def comments(self, number: int) -> list[dict]:
        pages = self._api(f"issues/{number}/comments?per_page=100", "--paginate", "--slurp") or []
        return [{"body": c["body"], "user": c["user"]["login"], "created_at": c["created_at"]}
                for page in pages for c in page]

    def issue_db_id(self, number: int) -> int:
        return int(self._api(f"issues/{number}")["id"])

    # -- native issue dependencies ("blocked by") --------------------------
    def blocked_by(self, number: int) -> dict[int, int]:
        """Blocker issue number -> blocker database id."""
        items = self._api(f"issues/{number}/dependencies/blocked_by?per_page=100") or []
        return {i["number"]: i["id"] for i in items}

    def blockers(self, number: int) -> dict[int, dict]:
        """Blocker issue number -> {"state": "open"|"closed", "reason": GitHub's state_reason}."""
        items = self._api(f"issues/{number}/dependencies/blocked_by?per_page=100") or []
        return {i["number"]: {"state": i["state"], "reason": i.get("state_reason")} for i in items}

    def blocking(self, number: int) -> list[int]:
        """Issues that ``number`` blocks: the backlinks GitHub keeps for every blocked-by link."""
        items = self._api(f"issues/{number}/dependencies/blocking?per_page=100") or []
        return [i["number"] for i in items]

    def issue(self, number: int) -> dict:
        """One issue's number, state and label names."""
        i = self._json("issue", "view", str(number), "-R", self.repo, "--json", "number,state,labels")
        i["labels"] = [l["name"] for l in i["labels"]]
        return i

    def add_blocked_by(self, number: int, blocker_db_id: int) -> None:
        self._json("api", "-X", "POST", f"repos/{self.repo}/issues/{number}/dependencies/blocked_by",
                   "-F", f"issue_id={blocker_db_id}")

    def remove_blocked_by(self, number: int, blocker_db_id: int) -> None:
        self._gh("api", "-X", "DELETE",
                 f"repos/{self.repo}/issues/{number}/dependencies/blocked_by/{blocker_db_id}")

    # -- pull requests ----------------------------------------------------
    def list_prs(self) -> list[dict]:
        return self._json("pr", "list", "-R", self.repo, "--label", PIPELINE_LABEL, "--state", "open",
                          "--limit", "500", "--json", "number,headRefName,headRefOid,body,title,files") or []

    def create_pr(self, branch: str, base: str, title: str, body: str) -> int:
        url = self._gh("pr", "create", "-R", self.repo, "--head", branch, "--base", base,
                       "--title", title, "--body-file", "-", "--label", PIPELINE_LABEL,
                       input=body).strip()
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def merge_pr(self, number: int, head_sha: str) -> None:
        # --match-head-commit makes the merge fail if anything was pushed after
        # the commit the pipeline tested.
        self._gh("pr", "merge", str(number), "-R", self.repo, "--squash", "--match-head-commit", head_sha)

    # -- releases ---------------------------------------------------------
    def create_release(self, tag: str, title: str, notes: str, assets: list[str]) -> None:
        """Publish a release for an already pushed tag, attaching ``assets`` (file paths)."""
        self._gh("release", "create", tag, *assets, "-R", self.repo, "--verify-tag",
                 "--title", title, "--notes-file", "-", input=notes)


class DryRunGitHub(GitHub):
    """Reads hit GitHub; writes are printed instead of performed."""

    def __init__(self, *a, log=print, **kw):
        super().__init__(*a, **kw)
        self._log = log
        self._next = 100000

    def _fake_number(self) -> int:
        self._next += 1
        return self._next

    def ensure_labels(self):
        self._log("[dry-run] ensure labels")

    def create_issue(self, title, body, status, extra_labels=()):
        n = self._fake_number()
        self._log(f"[dry-run] create issue #{n} [{status.value}] {title}")
        return n

    def set_status(self, number, new, old):
        self._log(f"[dry-run] #{number}: {old.value if old else '-'} -> {new.value}")

    def comment(self, number, body):
        self._log(f"[dry-run] comment on #{number}: {body.splitlines()[0][:100]}")

    def close_issue(self, number):
        self._log(f"[dry-run] close #{number}")

    def issue_db_id(self, number):
        return -number if number > 100000 else super().issue_db_id(number)

    def blocked_by(self, number):
        return {} if number > 100000 else super().blocked_by(number)

    def blockers(self, number):
        return {} if number > 100000 else super().blockers(number)

    def blocking(self, number):
        return [] if number > 100000 else super().blocking(number)

    def add_blocked_by(self, number, blocker_db_id):
        self._log(f"[dry-run] #{number} blocked by issue id {blocker_db_id}")

    def remove_blocked_by(self, number, blocker_db_id):
        self._log(f"[dry-run] #{number} no longer blocked by issue id {blocker_db_id}")

    def create_pr(self, branch, base, title, body):
        n = self._fake_number()
        self._log(f"[dry-run] open PR #{n} {branch} -> {base}: {title}")
        return n

    def merge_pr(self, number, head_sha):
        self._log(f"[dry-run] merge PR #{number} at {head_sha[:8]}")

    def create_release(self, tag, title, notes, assets):
        self._log(f"[dry-run] release {tag}: {title} ({len(assets)} asset(s))")
