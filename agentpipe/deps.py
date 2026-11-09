"""The dependency plan: which tasks block which, which need a human, which go together.

One plan covers a whole target (every change it includes), so a task may be
blocked by a task in another change. Tasks are named by key, ``change/id``.

The plan is updated whenever the target's task fingerprint changes. Only new or
reworded tasks (and the batches they touch) are sent to the planner; the rest of
the plan is kept. Validation is strict so that a bad planner answer fails loudly
instead of deadlocking or racing the queue.

Batches: small tasks of one change that are cheaper to build and review together
share a ``batch`` id. A batch runs as one job on its leader (the first open member):
one agent session, one branch, one PR that closes every member's issue. The leader is
ready once every member's blockers outside the batch are done; the other members stay
blocked until the PR merges and ticks them all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from agentpipe.conflicts import normalize
from agentpipe.tasks import Task

# How demanding a task is for the implementer: "light" runs on the cheaper model.
TIERS = ("light", "standard")

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
                    "batch": {"type": "string", "description": "shared id for tasks done in one PR; omit or empty for none"},
                    "touches": {"type": "array", "items": {"type": "string"},
                                "description": "repo-relative files or directories the task will most likely change"},
                    "tier": {"type": "string", "enum": list(TIERS)},
                },
                "required": ["key", "blocked_by", "autonomous", "tier"],
            },
        }
    },
    "required": ["tasks"],
}


TOUCHES_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"},
                               "touches": {"type": "array", "items": {"type": "string"}},
                               "tier": {"type": "string", "enum": list(TIERS)}},
                "required": ["key", "touches", "tier"],
            },
        }
    },
    "required": ["tasks"],
}


TIER_SCHEMA = {
    "type": "object",
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "tier": {"type": "string", "enum": list(TIERS)},
                               "unblocks": {"type": "boolean"}},
                "required": ["key", "tier", "unblocks"],
            },
        }
    },
    "required": ["tickets"],
}


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"},
                               "verdict": {"type": "string", "enum": ["approve", "ask", "watch", "recurs"]},
                               "same_as": {"type": "string",
                                           "description": "With recurs: the watched ticket's key"},
                               "reason": {"type": "string"}},
                "required": ["key", "verdict", "reason"],
            },
        }
    },
    "required": ["tickets"],
}


@dataclass
class TaskDeps:
    blocked_by: list[str] = field(default_factory=list)  # task keys
    autonomous: bool = True
    human_reason: str = ""
    batch: str = ""
    # Repo-relative files or directories the task will likely change; None = not predicted yet.
    touches: list[str] | None = None
    tier: str | None = None  # one of TIERS; None = not rated yet (runs as standard)


def text_hash(t: Task) -> str:
    return hashlib.sha256(t.text.encode()).hexdigest()[:12]


@dataclass
class Plan:
    fingerprint: str
    tasks: dict[str, TaskDeps]  # key -> deps
    texts: dict[str, str] = field(default_factory=dict)  # key -> text_hash when planned

    def get(self, key: str) -> TaskDeps:
        return self.tasks.get(key, TaskDeps())

    def members(self, batch: str) -> list[str]:
        return sorted((k for k, d in self.tasks.items() if batch and d.batch == batch), key=key_sort)

    def batch_of(self, key: str, done: set[str] = frozenset()) -> list[str]:
        """Open members of ``key``'s batch, leader first; just [key] when it has none."""
        b = self.get(key).batch
        return [k for k in self.members(b) if k not in done] if b else [key]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "fingerprint": self.fingerprint,
            "tasks": {
                k: vars(v) for k, v in sorted(self.tasks.items(), key=lambda kv: key_sort(kv[0]))
            },
            "texts": dict(sorted(self.texts.items(), key=lambda kv: key_sort(kv[0]))),
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Plan | None":
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["fingerprint"], {k: TaskDeps(**v) for k, v in data["tasks"].items()},
                   dict(data.get("texts", {})))


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


def stale_keys(tasks: list[Task], plan: Plan) -> tuple[set[str], set[str]]:
    """(open tasks the planner must see again, planned keys whose task is gone).

    A task is stale when it is new or its text changed. Every open member of a batch
    that has a stale or removed member is stale too, so the batch is reconsidered whole.
    """
    by_key = {t.key: t for t in tasks}
    removed = {k for k in plan.tasks if k not in by_key}
    stale = {t.key for t in tasks if not t.done
             and (t.key not in plan.tasks or plan.texts.get(t.key) != text_hash(t))}
    touched = {plan.get(k).batch for k in stale | removed if plan.get(k).batch}
    stale |= {k for b in touched for k in plan.members(b) if k in by_key and not by_key[k].done}
    return stale, removed


def merge_plans(old: Plan, answer: dict, stale: set[str], removed: set[str]) -> dict:
    """Old plan entries plus the planner's answer, as a raw answer for ``validate_plan``.

    The answer must cover every stale task and may also replace entries of existing
    tasks (e.g. to make one wait for a new task). Removed tasks vanish everywhere.
    """
    entries = {k: {"key": k, **vars(d)} for k, d in old.tasks.items() if k not in removed and k not in stale}
    for e in answer.get("tasks", []):
        entries[str(e.get("key", "")).strip()] = e
    for e in entries.values():
        e["blocked_by"] = [b for b in e.get("blocked_by", []) if str(b).strip() not in removed]
    for b in {e.get("batch") for e in entries.values() if e.get("batch")}:
        live = [e for e in entries.values() if e.get("batch") == b]
        if len(live) == 1:  # a batch left with one member is no batch
            live[0]["batch"] = ""
    return {"tasks": list(entries.values())}


def validate_plan(tasks: list[Task], raw: dict, fingerprint: str, max_batch: int = 4) -> Plan:
    """Turn a planner answer into a Plan, or raise PlanError listing every problem.

    Rules: every open task is covered exactly once; blockers are known task
    keys and not the task itself; open tasks form no cycle, also once each batch is
    collapsed into one node (a done task can't be part of one, since a done blocker is
    already satisfied); a batch holds 2..max_batch open, autonomous tasks of one change.
    """
    by_key = {t.key: t for t in tasks}
    known = set(by_key)
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
        batch = str(entry.get("batch", "") or "").strip() if max_batch > 1 else ""
        out[key] = TaskDeps(sorted(blockers, key=key_sort), autonomous, reason, batch,
                            clean_touches(entry.get("touches")), clean_tier(entry.get("tier")))
    missing = open_keys - out.keys()
    if missing:
        errors.append("open tasks missing from plan: " + ", ".join(sorted(missing, key=key_sort)))
    batches: dict[str, list[str]] = {}
    for k, d in out.items():
        if d.batch and k in open_keys:
            batches.setdefault(d.batch, []).append(k)
        elif d.batch:
            d.batch = ""  # done tasks need no batch
    for b, ks in batches.items():
        if len(ks) < 2:
            out[ks[0]].batch = ""
            continue
        if len(ks) > max_batch:
            errors.append(f"batch {b!r} has {len(ks)} tasks; at most {max_batch}")
        if len({by_key[k].change for k in ks}) > 1:
            errors.append(f"batch {b!r} mixes changes")
        if bad := [k for k in ks if not out[k].autonomous]:
            errors.append(f"batch {b!r} holds non-autonomous {', '.join(bad)}")
    if not errors:
        node = {k: f"batch:{d.batch}" if d.batch else k for k, d in out.items() if k in open_keys}
        edges: dict[str, list[str]] = {n: [] for n in node.values()}
        for k, d in out.items():
            if k in open_keys:
                edges[node[k]] += [node[b] for b in d.blocked_by if b in open_keys and node[b] != node[k]]
        if cycle := find_cycle(edges):
            errors.append("dependency cycle: " + " -> ".join(cycle))
    if errors:
        raise PlanError("; ".join(errors))
    return Plan(fingerprint, out, {k: text_hash(by_key[k]) for k in out})


def clean_touches(raw) -> list[str] | None:
    """A planner's `touches` as sorted, normalized paths; None when it gave none."""
    if raw is None:
        return None
    return sorted({p for p in (normalize(str(x)) for x in raw) if p})


def clean_tier(raw) -> str | None:
    """A planner's `tier`, or None when it gave none or an unknown one."""
    t = str(raw or "").strip().lower()
    return t if t in TIERS else None


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

    for n in sorted(edges, key=lambda n: key_sort(n.removeprefix("batch:"))):
        if color[n] == WHITE and (found := visit(n)):
            return found
    return None


def classify(task: Task, plan: Plan, done_keys: set[str]) -> str:
    """Queue status for a task nobody has claimed: ready, blocked or needs-human.

    A non-autonomous task asks the human only once its blockers are done: there is nothing to
    decide or play before then."""
    d = plan.get(task.key)
    if not d.autonomous:
        return "needs-human" if all(b in done_keys for b in d.blocked_by) else "blocked"
    group = plan.batch_of(task.key, done_keys)
    if group[0] != task.key:
        return "blocked"  # a batch follower: its leader's PR does it
    outside = {b for k in group for b in plan.get(k).blocked_by if b not in group}
    if all(b in done_keys for b in outside):
        return "ready"
    return "blocked"
