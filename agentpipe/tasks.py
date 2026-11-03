"""Parse OpenSpec tasks.md checklists and flip their checkboxes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

TASK_RE = re.compile(
    r"^(?P<indent>\s*)-\s\[(?P<done>[ xX])\]\s+(?P<id>\d+(?:\.\d+)*[a-z]?)\s+(?P<text>\S.*?)\s*$"
)
SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")


class TaskParseError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    change: str
    id: str
    text: str
    done: bool
    section: str
    line: int  # 1-based

    @property
    def key(self) -> str:
        return f"{self.change}/{self.id}"

    @property
    def slug(self) -> str:
        return f"{self.change}-{self.id}".replace(".", "_")

    @property
    def branch(self) -> str:
        return f"pipeline/{self.change}/{self.id}"


def parse_tasks(change: str, text: str) -> list[Task]:
    """Return every numbered checkbox task in a tasks.md, in file order.

    Only lines of the form ``- [ ] 1.2 text`` count; prose, blockquotes and
    unnumbered checkboxes are ignored. Duplicate ids are an error, because the
    id is the task's identity everywhere downstream.
    """
    tasks: list[Task] = []
    seen: dict[str, int] = {}
    section = ""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if m := SECTION_RE.match(line):
            section = m["title"]
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        task_id = m["id"]
        if task_id in seen:
            raise TaskParseError(
                f"{change}: task {task_id} appears on lines {seen[task_id]} and {lineno}"
            )
        seen[task_id] = lineno
        tasks.append(
            Task(change, task_id, m["text"], m["done"] in "xX", section, lineno)
        )
    return tasks


def mark_done(text: str, task_id: str) -> str:
    """Tick the checkbox of ``task_id``. Raises if the task is not present."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = TASK_RE.match(line.rstrip("\r\n"))
        if m and m["id"] == task_id:
            start = m.start("done")
            lines[i] = line[:start] + "x" + line[start + 1 :]
            return "".join(lines)
    raise TaskParseError(f"task {task_id} not found")


def fingerprint(tasks: list[Task]) -> str:
    """Hash of task ids and texts, ignoring done state.

    Ticking a box must not trigger re-planning; rewording or adding a task must.
    """
    h = hashlib.sha256()
    for t in tasks:
        h.update(f"{t.id}\t{t.text}\n".encode())
    return h.hexdigest()[:16]
