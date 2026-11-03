"""Milestone releases: decide when a milestone is complete and which commit to cut it from."""

from __future__ import annotations

from pathlib import Path

from agentpipe.config import Milestone
from agentpipe.tasks import parse_tasks

CHANGES = Path("openspec") / "changes"


def change_done(checkout: Path, change: str) -> bool:
    """Every task of ``change`` is ticked, or the change has been archived."""
    path = checkout / CHANGES / change / "tasks.md"
    if path.exists():
        tasks = parse_tasks(change, path.read_text(encoding="utf-8"))
        return bool(tasks) and all(t.done for t in tasks)
    archive = checkout / CHANGES / "archive"
    return archive.is_dir() and any(p.name.endswith("-" + change) for p in archive.iterdir())


def next_due(milestones: list[Milestone], released: set[str], checkout: Path) -> Milestone | None:
    """The first unreleased milestone, if it is complete. Milestones are cut strictly in order."""
    for m in milestones:
        if m.tag in released:
            continue
        return m if all(change_done(checkout, c) for c in m.changes) else None
    return None


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
