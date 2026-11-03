"""The dependency plan: which tasks block which, and which need a human.

One plan covers a whole target (every change it includes), so a task may be
blocked by a task in another change. Tasks are named by key, ``change/id``.
The plan is regenerated whenever the target's task fingerprint changes.
Validation is strict so that a bad planner answer fails loudly instead of
deadlocking or racing the queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agentpipe.tasks import Task

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "change/id"},
                    "blocked_by": {"type": "array", "items": {"type": "string"}},
                    "autonomous": {"type": "boolean"},
                    "human_reason": {"type": "string"},
                },
                "required": ["key", "blocked_by", "autonomous"],
            },
        }
    },
    "required": ["tasks"],
}


@dataclass
class TaskDeps:
    blocked_by: list[str] = field(default_factory=list)  # task keys
    autonomous: bool = True
    human_reason: str = ""


@dataclass
class Plan:
    fingerprint: str
    tasks: dict[str, TaskDeps]  # key -> deps

    def get(self, key: str) -> TaskDeps:
        return self.tasks.get(key, TaskDeps())

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "fingerprint": self.fingerprint,
            "tasks": {
                k: vars(v) for k, v in sorted(self.tasks.items(), key=lambda kv: key_sort(kv[0]))
            },
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Plan | None":
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["fingerprint"], {k: TaskDeps(**v) for k, v in data["tasks"].items()})


class PlanError(ValueError):
    pass


def id_sort_key(task_id: str) -> tuple:
    """Order 1.2 < 1.10 < 2.1 < 2.4a < 2.4b."""
    parts = []
    for p in task_id.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append((int(digits) if digits else 0, p[len(digits):]))
    return tuple(parts)


def key_sort(key: str) -> tuple:
    change, _, tid = key.rpartition("/")
    return (change, id_sort_key(tid))


def validate_plan(tasks: list[Task], raw: dict, fingerprint: str) -> Plan:
    """Turn a planner answer into a Plan, or raise PlanError listing every problem.

    Rules: every open task is covered exactly once; blockers are known task
    keys and not the task itself; open tasks form no cycle (a done task can't
    be part of one, since a done blocker is already satisfied).
    """
    known = {t.key for t in tasks}
    open_keys = {t.key for t in tasks if not t.done}
    errors: list[str] = []
    out: dict[str, TaskDeps] = {}
    for entry in raw.get("tasks", []):
        key = str(entry.get("key", "")).strip()
        if key not in known:
            errors.append(f"unknown task {key!r}")
            continue
        if key in out:
            errors.append(f"{key} listed twice")
            continue
        blockers: list[str] = []
        for b in entry.get("blocked_by", []):
            b = str(b).strip()
            if b == key:
                errors.append(f"{key} blocks itself")
            elif b not in known:
                errors.append(f"{key} blocked by unknown task {b!r}")
            elif b not in blockers:
                blockers.append(b)
        autonomous = bool(entry.get("autonomous", True))
        reason = str(entry.get("human_reason", "") or "").strip()
        if not autonomous and not reason:
            errors.append(f"{key} marked non-autonomous without a human_reason")
        out[key] = TaskDeps(sorted(blockers, key=key_sort), autonomous, reason)
    missing = open_keys - out.keys()
    if missing:
        errors.append("open tasks missing from plan: " + ", ".join(sorted(missing, key=key_sort)))
    if not errors:
        edges = {k: [b for b in d.blocked_by if b in open_keys] for k, d in out.items() if k in open_keys}
        if cycle := find_cycle(edges):
            errors.append("dependency cycle: " + " -> ".join(cycle))
    if errors:
        raise PlanError("; ".join(errors))
    return Plan(fingerprint, out)


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as a closed path (a -> b -> a), or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GREY
        stack.append(n)
        for m in edges.get(n, []):
            c = color.get(m, BLACK)  # nodes without out-edges can't close a cycle
            if c == GREY:
                return stack[stack.index(m):] + [m]
            if c == WHITE and (found := visit(m)):
                return found
        stack.pop()
        color[n] = BLACK
        return None

    for n in sorted(edges, key=key_sort):
        if color[n] == WHITE and (found := visit(n)):
            return found
    return None


def classify(task: Task, plan: Plan, done_keys: set[str]) -> str:
    """Queue status for a task nobody has claimed: ready, blocked or needs-human."""
    d = plan.get(task.key)
    if not d.autonomous:
        return "needs-human"
    if all(b in done_keys for b in d.blocked_by):
        return "ready"
    return "blocked"
