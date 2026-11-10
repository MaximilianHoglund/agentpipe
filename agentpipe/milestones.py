"""Milestone releases: decide when a milestone is complete and which commit to cut it from."""

from __future__ import annotations

from pathlib import Path

from agentpipe.config import Milestone
from agentpipe.status import TICKET_CHANGE
from agentpipe.tasks import parse_tasks

CHANGES = Path("openspec") / "changes"
# Rank of a task in the target's `priority` list: after unblocking tickets (-1), before the rest.
PRIORITY_RANK = -0.5


def change_done(checkout: Path, change: str, skip: frozenset[str] | set[str] = frozenset()) -> bool:
    """Every task of ``change`` not in ``skip`` is ticked, or the change has been archived."""
    path = checkout / CHANGES / change / "tasks.md"
    if path.exists():
        tasks = parse_tasks(change, path.read_text(encoding="utf-8"))
        return bool(tasks) and all(t.done or t.key in skip for t in tasks)
    archive = checkout / CHANGES / "archive"
    return archive.is_dir() and any(p.name.endswith("-" + change) for p in archive.iterdir())


def next_due(milestones: list[Milestone], released: set[str], checkout: Path) -> Milestone | None:
    """The first unreleased milestone, if it is complete. Milestones are cut strictly in order.

    A milestone's own play tasks don't count, since they need the release to exist, and neither
    do its deferred tasks."""
    for m in milestones:
        if m.tag in released:
            continue
        skip = set(m.play) | set(m.deferred)
        return m if all(change_done(checkout, c, skip) for c in m.changes) else None
    return None


def play_tags(milestones: list[Milestone]) -> dict[str, str]:
    """Task key -> the release tag the owner plays for it."""
    return {k: m.tag for m in milestones for k in m.play}


def rank(milestones: list[Milestone], change: str) -> int:
    """Scheduling priority of a task in ``change``: lower runs first.

    Work toward an earlier milestone goes before work toward a later one, and work in no milestone
    goes last. ``schedule`` ranks tickets with ``ticket_rank``; this ranks a bare ``ticket``
    change first, as an unrated ticket that unblocks work.
    """
    if change == TICKET_CHANGE:
        return -1
    for i, m in enumerate(milestones):
        if change in m.changes:
            return i
    return len(milestones)


def ticket_rank(milestones: list[Milestone], origin: str | None, unblocks: bool,
                released: set[str] | frozenset[str] = frozenset()) -> float:
    """Scheduling priority of an approved ticket, on the same scale as ``rank``.

    A ticket that fixes something broken now (the gate, build, export or release) goes before
    everything. Any other ticket goes after the tasks of the milestone its origin belongs to, and
    before the next milestone's: it was filed while that work was done, but that work doesn't
    need it. With no known origin it counts as coming from the first milestone.

    Once that milestone is released (its tag in ``released``), its tickets no longer hold the
    next step back: they go after the tasks of the first unreleased milestone instead (Max,
    2026-11-10: Step 1's leftover tickets kept Step 2 from starting after its playtest).
    """
    if unblocks:
        return -1
    base = rank(milestones, origin) if origin else 0
    first_open = next((i for i, m in enumerate(milestones) if m.tag not in released), len(milestones))
    return max(base, first_open) + 0.5


def task_paths(m: Milestone) -> list[str]:
    """Paths whose last commit marks the moment the milestone completed.

    Archiving a change deletes its tasks.md, which also counts as touching it.
    """
    return [f"{CHANGES.as_posix()}/{c}/tasks.md" for c in m.changes]


def notes(m: Milestone, base: str, prev_tag: str | None, log: str) -> str:
    lines = [f"Milestone **{m.title}**, cut from `{base}` by the pipeline.", "",
             "Changes completed:", *[f"- `{c}`" for c in m.changes], ""]
    since = f"since `{prev_tag}`" if prev_tag else "so far"
    lines += [f"Commits {since}:", "", "```", log.strip() or "(none)", "```"]
    return "\n".join(lines)
