"""Milestone releases: decide when a milestone is complete and which commit to cut it from."""

from __future__ import annotations

from pathlib import Path

from agentpipe.config import Milestone
from agentpipe.status import TICKET_CHANGE
from agentpipe.tasks import parse_tasks

CHANGES = Path("openspec") / "changes"


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

    A milestone's own play tasks don't count: they need the release to exist."""
    for m in milestones:
        if m.tag in released:
            continue
        return m if all(change_done(checkout, c, set(m.play)) for c in m.changes) else None
    return None


def play_tags(milestones: list[Milestone]) -> dict[str, str]:
    """Task key -> the release tag the owner plays for it."""
    return {k: m.tag for m in milestones for k in m.play}


def rank(milestones: list[Milestone], change: str) -> int:
    """Scheduling priority of a task in ``change``: lower runs first.

    Work toward an earlier milestone goes before work toward a later one, and work in no milestone
    goes last. Tickets rank first, since they fix what blocks the work in progress.
    """
    if change == TICKET_CHANGE:
        return -1
    for i, m in enumerate(milestones):
        if change in m.changes:
            return i
    return len(milestones)


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
