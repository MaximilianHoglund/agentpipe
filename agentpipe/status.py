"""Task statuses, their GitHub labels, and the issue/PR body markers."""

from __future__ import annotations

import re
from enum import Enum


class Status(str, Enum):
    BLOCKED_BY_TASK = "blocked by task"
    NEEDS_DECISION = "needs decision"
    PROPOSED = "proposed"
    WATCHING = "watching"
    AWAITING_TASK = "awaiting task"
    PENDING_IMPLEMENTATION = "pending implementation"
    IN_PROGRESS = "in progress"
    PENDING_REVIEW = "pending review"
    CHANGES_REQUESTED = "changes requested"
    APPROVED = "approved"
    FINISHED = "finished"

    @property
    def label(self) -> str:
        return f"status: {self.value}"


LABEL_INFO = {
    Status.BLOCKED_BY_TASK: ("bfd4f2", "Waits on another task; see the issue's blocked-by links"),
    Status.NEEDS_DECISION: ("d93f0b", "Needs information or a decision from a human; see the latest comment"),
    Status.PROPOSED: ("f9d0c4", "Agent-filed ticket; waits for a human (or triage) to approve it"),
    Status.WATCHING: ("ededed", "A one-off problem tracked aside; becomes work once it recurs"),
    Status.AWAITING_TASK: ("c2e0c6", "Decided; waits for another task to land, named in the latest comment"),
    Status.PENDING_IMPLEMENTATION: ("0e8a16", "Unblocked; waiting for an implementer agent"),
    Status.IN_PROGRESS: ("fbca04", "An implementer agent is working on it"),
    Status.PENDING_REVIEW: ("1d76db", "PR open; waiting for the reviewer agent"),
    Status.CHANGES_REQUESTED: ("e99695", "Reviewer rejected the PR; implementer is revising"),
    Status.APPROVED: ("5319e7", "Reviewer approved; being merged"),
    Status.FINISHED: ("000000", "Merged"),
}
PIPELINE_LABEL = "pipeline"

# Statuses the sync step may recompute from the dependency graph. Everything
# else is owned by a running agent or by a human and is left alone.
GRAPH_OWNED = {Status.BLOCKED_BY_TASK, Status.PENDING_IMPLEMENTATION}

# Until 2026-11-08 one label, "pending approval", meant both a PR waiting for review and a
# ticket waiting for a human. ensure_labels renames it on GitHub to "pending review"; until
# then it reads as pending review, and the dispatcher moves untouched tickets to proposed.
LEGACY_PENDING_APPROVAL = "status: pending approval"
_BY_LABEL = {s.label: s for s in Status} | {LEGACY_PENDING_APPROVAL: Status.PENDING_REVIEW}


def status_from_labels(labels: list[str]) -> Status | None:
    found = [_BY_LABEL[name] for name in labels if name in _BY_LABEL]
    if not found:
        return None
    # Two status labels means a human edited by hand; the most "human" one wins.
    order = [Status.NEEDS_DECISION, Status.PROPOSED, Status.WATCHING, Status.AWAITING_TASK, Status.FINISHED, Status.APPROVED,
             Status.CHANGES_REQUESTED, Status.PENDING_REVIEW, Status.IN_PROGRESS, Status.PENDING_IMPLEMENTATION,
             Status.BLOCKED_BY_TASK]
    return min(found, key=order.index)


TASK_MARKER = "<!-- pipeline-task: {key} -->"
_TASK_MARKER_RE = re.compile(r"<!-- pipeline-task: (?P<key>\S+) -->")
REVIEW_MARKER = "<!-- pipeline-review sha={sha} verdict={verdict} round={round} -->"
_REVIEW_MARKER_RE = re.compile(
    r"<!-- pipeline-review sha=(?P<sha>\w+) verdict=(?P<verdict>[\w-]+) round=(?P<round>\d+) -->"
)
# Issues filed by agents (defects/improvements) rather than taken from tasks.md.
# Their task key is ticket/<issue number>.
TICKET_MARKER = "<!-- pipeline-ticket -->"
TICKET_CHANGE = "ticket"
KIND_LABELS = {"defect": ("b60205", "Defect an agent noticed"),
               "improvement": ("c5def5", "Improvement an agent proposed")}

# Comments the pipeline writes itself; everything else on an issue is human input.
BOT_MARKER = "<!-- pipeline-bot -->"


def task_key_from_body(body: str | None) -> str | None:
    m = _TASK_MARKER_RE.search(body or "")
    return m["key"] if m else None


def parse_review_marker(body: str | None) -> dict | None:
    m = _REVIEW_MARKER_RE.search(body or "")
    if not m:
        return None
    return {"sha": m["sha"], "verdict": m["verdict"], "round": int(m["round"])}


def is_bot_comment(body: str | None) -> bool:
    b = body or ""
    return BOT_MARKER in b or _REVIEW_MARKER_RE.search(b) is not None
