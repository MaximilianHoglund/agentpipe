"""Decide which issue changes a sync needs. Pure: no I/O, so it is unit-tested directly."""

from __future__ import annotations

from dataclasses import dataclass

from agentpipe.deps import Plan, classify, key_sort
from agentpipe.status import GRAPH_OWNED, Status, status_from_labels
from agentpipe.tasks import Task

# Statuses meaning an agent owns the task right now; a human tick in tasks.md
# does not close these, because the agent's PR would then land on a closed issue.
AGENT_OWNED = {Status.IN_PROGRESS, Status.PENDING_APPROVAL, Status.CHANGES_REQUESTED, Status.APPROVED}

_STATUS_FOR = {
    "ready": Status.PENDING_IMPLEMENTATION,
    "blocked": Status.BLOCKED_BY_TASK,
    "needs-human": Status.NEEDS_DECISION,
}


@dataclass(frozen=True)
class Create:
    key: str
    status: Status
    reason: str = ""  # for NEEDS_DECISION: what the human must provide


@dataclass(frozen=True)
class SetStatus:
    key: str
    number: int
    old: Status | None
    new: Status
    reason: str = ""


@dataclass(frozen=True)
class Close:
    key: str
    number: int
    old: Status | None


@dataclass(frozen=True)
class Cleared:
    """A human moved a planner-flagged task out of needs-decision: stop re-flagging it."""
    key: str


def compute(tasks: list[Task], plan: Plan, issues: dict[str, dict],
            flagged: set[str], cleared: set[str]) -> list:
    """Actions that bring GitHub in line with tasks.md and the plan.

    ``issues`` maps task key -> issue dict (number, state, labels). ``flagged`` holds
    keys the pipeline itself put in needs-decision; if such an issue is now back in a
    graph-owned status, a human cleared it, and the planner's verdict is overridden.
    """
    done = {t.key for t in tasks if t.done}
    done |= {k for k, i in issues.items() if i["state"].upper() == "CLOSED"}
    actions: list = []
    cleared = set(cleared)
    for t in sorted(tasks, key=lambda t: key_sort(t.key)):
        issue = issues.get(t.key)
        cur = status_from_labels(issue["labels"]) if issue else None
        if t.done:
            if issue and issue["state"].upper() == "OPEN" and cur not in AGENT_OWNED:
                actions.append(Close(t.key, issue["number"], cur))
            continue
        if issue and t.key in flagged and cur in GRAPH_OWNED:
            cleared.add(t.key)
            actions.append(Cleared(t.key))
        new = _status_for(t, plan, done, cleared)
        reason = plan.get(t.key).human_reason if new is Status.NEEDS_DECISION else ""
        if issue is None:
            actions.append(Create(t.key, new, reason))
        elif issue["state"].upper() == "OPEN" and (cur is None or cur in GRAPH_OWNED) and new != cur:
            actions.append(SetStatus(t.key, issue["number"], cur, new, reason))
    return actions


def _status_for(t: Task, plan: Plan, done: set[str], cleared: set[str]) -> Status:
    if t.key in cleared and not plan.get(t.key).autonomous:
        plan = Plan(plan.fingerprint, {**plan.tasks, t.key: type(plan.get(t.key))(
            plan.get(t.key).blocked_by, True, "")})
    return _STATUS_FOR[classify(t, plan, done)]
