"""Load pipeline.toml: shared settings plus one [targets.<name>] table per project."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Agent:
    model: str
    max_turns: int
    tools: list[str]
    # Denied even though the tool is allowed; deny wins over allow in Claude Code.
    deny: list[str] = field(default_factory=list)
    # A cheaper model for the light jobs (see Pipeline.model_for); "" runs everything on `model`.
    light_model: str = ""


@dataclass
class Milestone:
    tag: str  # git tag and GitHub release name, e.g. "step-1-watchable-combat"
    title: str
    changes: list[str]  # OpenSpec changes that must all be ticked off
    # Task keys where the owner plays this release: they wait for its tag, and the release
    # doesn't wait for them (a task inside the milestone would otherwise deadlock it).
    play: list[str] = field(default_factory=list)
    # Task keys the release doesn't wait for, with no other effect: they still run whenever they
    # can (e.g. tasks that need a machine that is unavailable).
    deferred: list[str] = field(default_factory=list)


@dataclass
class Target:
    name: str
    repo_root: Path
    github_repo: str  # owner/name
    base: str
    changes_spec: list[str] | str  # list of change names, or "*" for every non-archived change
    project_dir: str  # relative to repo_root; agents and the test gate run here
    test: str  # shell command, must exit 0
    mutation: str  # optional evidence command; {diff} -> path of the PR diff
    context: list[str]  # extra docs (repo-relative) the planner and agents read first
    work_dir: Path
    test_timeout: int = 3600
    mutation_timeout: int = 3600
    # True starts the mutation run alongside the reviewer's gate instead of after it. Only for a
    # mutation command that works on copies of the tree: an `--in-place` run edits the files the
    # gate is building.
    mutation_alongside_gate: bool = False
    # What the mutation command reaches, in words, and what it leaves out, e.g. "Rust outside
    # crates/bridge; not crates/bridge or GDScript". The implementer is told to probe the rest by
    # hand: without it, agents read "mutation runs on your lines" as covering everything.
    mutation_covers: str = ""
    # Up to this many approved PRs are merged together onto the base in a scratch worktree,
    # gated once, then squash-merged one by one. 1 merges each PR alone with its own gate.
    merge_batch: int = 1
    # Appended to the implementer's and reviewer's allowed tools for this target only.
    extra_tools: list[str] = field(default_factory=list)
    # Extra environment variables for this target's agents, gate and mutation runs.
    env: dict[str, str] = field(default_factory=dict)
    # Slots reuse one cargo target dir across branches; cargo can then take another branch's
    # artifact for a same-path workspace crate as fresh. True cleans the workspace's own
    # crates (not dependencies) from the slot's target before each gate and agent run.
    clean_workspace_crates: bool = False
    # Cap in GB on the per-slot build and temp dirs (work_dir/target/* and work_dir/tmp/*)
    # together. Over it, the dispatcher deletes idle slots' dirs, largest first, down to 3/4
    # of the cap; a deleted slot rebuilds from scratch. 0 means no cap.
    cache_max_gb: float = 0.0
    # Milestone releases: when every change of the next milestone is done on `base`,
    # merge it into `release_branch`, tag it and publish a GitHub release.
    release_branch: str = ""
    milestones: list[Milestone] = field(default_factory=list)
    release_build: str = ""  # optional command run in the merged checkout before tagging
    # The release's gate, if not `test`: e.g. one that also runs tests the per-PR gate leaves out.
    release_test: str = ""
    release_assets: list[str] = field(default_factory=list)  # globs (project-relative) to attach
    # Checked before any agent runs (dispatcher start, then each sync while failing):
    # each command must exit 0 from the dispatcher's shell and from an agent's Bash tool.
    preflight: list[str] = field(default_factory=list)
    preflight_gate: bool = False  # also run `test` on the base branch
    # Run in project_dir after the dispatcher resolves a merge conflict where both sides only
    # added lines (it keeps both): e.g. `cargo fmt --all` re-sorts the `mod`/`use` lines it
    # appended. Leave empty when the base branch isn't kept formatted.
    format: str = ""
    # A quick check agents run in place of the full `test` (e.g. a compile and lint of every
    # target), plus the tests for what they changed. With it set, the pipeline runs `test` itself
    # on every branch merged with the base before review, and sends failures back to the
    # implementer's session. Empty: agents run the full `test` themselves.
    quick_check: str = ""
    # Two tracks (Max, 2026-11-09): engine work and the front end (arpg: Godot), each with its own
    # implementer slots, so neither holds the other up. A task is `front` when a path it touches
    # (the planner's `touches`, a ticket's named files) matches one of `front_paths` (fnmatch
    # globs, repo-relative); every other task is `engine`. The last `front_implementers` implementer
    # slots take only front tasks, the others only engine tasks. 0 keeps one shared pool.
    front_paths: list[str] = field(default_factory=list)
    front_implementers: int = 0
    # A full gate on the base branch, run when it has moved and at most every `dev_gate_minutes`.
    # When it fails, the pipeline files an approved front ticket with the output (or comments on
    # the open one), so a gate that leaves out the front's tests for engine changes still catches
    # what they broke, without holding the engine's merges (Max, 2026-11-09). Empty: off.
    dev_gate: str = ""
    dev_gate_minutes: int = 60
    # Appended to the dev gate's ticket: the project's own rule for fixing such a failure.
    dev_gate_note: str = ""
    # Paths (fnmatch globs, repo-relative) that don't count when the scheduler keeps tasks
    # touching the same files apart: files nearly every task edits and whose conflicts are
    # additive, such as module lists and decision logs.
    overlap_ignore: list[str] = field(default_factory=list)
    # Items Max has parked (issue numbers or task keys): they stay as they are, but don't count
    # as waiting on him. Milestone `deferred` tasks count as parked too.
    parked: list[int | str] = field(default_factory=list)
    # Task keys Max wants done first: they start before every other task and ticket, after
    # only tickets that unblock the gate or release. List what they wait on too.
    priority: list[str] = field(default_factory=list)
    # Work to run alone (Max, 2026-11-09): entries are task keys, or a change name that matches the
    # change's tasks and the tickets whose origin is that change. While any of them is open,
    # or waiting in the inbox, only that work is started (implement, review, merge), and no dev gate
    # or release; then the dispatcher carries on as usual by itself. Remove the entry once done.
    exclusive: list[str] = field(default_factory=list)
    # Run at the repo root once every task of a change is ticked: syncs its spec deltas into
    # openspec/specs/ and moves it to changes/archive/. {change} is the change name; "" turns
    # archiving off.
    archive: str = "openspec archive {change} --yes"
    # Agent-filed tickets get one triage run: purely technical ones are approved without Max,
    # the rest wait for him in proposed with the triage's reason as a comment.
    auto_approve: bool = False
    # Triage tracks one-off problems (a flaky test seen once) as `watching` rather than
    # approving them; a watched problem becomes work once it has been seen this many times.
    watch_promote_at: int = 3
    # Implementer slots unblocking tickets may hold at once, so a burst of them can't push
    # milestone work out (Max, 2026-11-09).
    unblocking_slots: int = 1
    # Per-target agent counts, overriding [run] (e.g. a smaller machine): None keeps [run]'s.
    max_implementers: int | None = None
    max_reviewers: int | None = None

    def is_parked(self, key: str, number: int) -> bool:
        return (number in self.parked or key in self.parked
                or any(key in m.deferred for m in self.milestones))

    @property
    def changes_dir(self) -> Path:
        return Path("openspec") / "changes"

    def changes(self, checkout: Path) -> list[str]:
        """Changes this target covers, resolved against a checkout of the repo."""
        root = checkout / self.changes_dir
        if self.changes_spec == "*":
            return sorted(
                p.name for p in root.iterdir()
                if p.is_dir() and p.name != "archive" and (p / "tasks.md").exists()
            )
        # A listed change that has been archived since has no tasks.md left to load.
        return [c for c in self.changes_spec if (root / c / "tasks.md").exists()]


@dataclass
class Config:
    targets: dict[str, Target]
    claude_bin: str
    gh_bin: str
    agents: dict[str, Agent]
    max_implementers: int = 2
    max_reviewers: int = 1
    max_rounds: int = 3
    poll_seconds: int = 60
    sync_seconds: int = 600
    limit_backoff_seconds: int = 1800
    # Start nothing new once a usage window (as agents report it) reaches this share; resume
    # when it resets. Running jobs finish, so leave them headroom below 1.0.
    throttle: dict[str, float] = field(default_factory=lambda: {"five_hour": 0.85, "seven_day": 0.93})
    agent_timeout: int = 4 * 3600
    # Resume an agent's last session only if it ended this recently; an older one is
    # re-cached in full and carries stale context, so start fresh with a summary instead.
    resume_within_seconds: int = 3000
    max_batch: int = 4  # most tasks the planner may put in one batch (one PR); 1 turns batching off
    mcp_servers: dict = field(default_factory=dict)  # the only MCP servers agents get
    # Agent runs get an environment in which gh is logged out and git push fails (see
    # claude.guard_env); [permissions] env_guard = false turns it off.
    env_guard: bool = True
    extra: dict = field(default_factory=dict)
    # The [machines.<name>] section in effect ("" when none matched), and the targets it may
    # run (None: any). A target runs on one machine at a time, so each machine lists its own.
    machine: str = ""
    machine_targets: list[str] | None = None

    def runs_here(self, target: str) -> bool:
        return self.machine_targets is None or target in self.machine_targets

    def __post_init__(self) -> None:
        # The preflight agent only runs the targets' check commands, so a small model will
        # do; it gets the implementer's tools and denials, since those are what it checks.
        impl = self.agents.get("implementer")
        pre = self.agents.get("preflight")
        if impl and pre is None:
            self.agents["preflight"] = Agent("claude-haiku-5-5", 20, list(impl.tools), list(impl.deny))
        elif impl and pre and not pre.tools:
            pre.tools = list(impl.tools)


def machine_name(machines: dict) -> str:
    """The [machines.<name>] section for this machine: AGENTPIPE_MACHINE if set, else the
    section whose `hostnames` lists this host, else ""."""
    if name := os.environ.get("AGENTPIPE_MACHINE", ""):
        if name not in machines:
            raise ValueError(f"AGENTPIPE_MACHINE={name!r}: pipeline.toml has no [machines.{name}]")
        return name
    host = socket.gethostname().lower()
    for name, m in machines.items():
        if host in (h.lower() for h in m.get("hostnames", [])):
            return name
    return ""


def apply_machine(data: dict) -> tuple[str, list[str] | None]:
    """Merge this machine's overrides into the parsed file: top-level keys (claude_bin, gh_bin,
    work_root) and per-target keys under [machines.<name>.target.<target>]."""
    machines = data.get("machines", {})
    name = machine_name(machines)
    if not name:
        return "", None
    m = machines[name]
    for key in ("claude_bin", "gh_bin", "work_root"):
        if key in m:
            data[key] = m[key]
    for tname, over in m.get("target", {}).items():
        if tname not in data.get("targets", {}):
            raise ValueError(f"[machines.{name}.target.{tname}]: no such target")
        data["targets"][tname].update(over)
    targets = m.get("targets")
    return name, (list(targets) if targets is not None else None)


def load(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    machine, machine_targets = apply_machine(data)
    run = data.get("run", {})
    work_root = Path(os.path.expanduser(data.get("work_root", "E:/Source/.pipeline")))
    shared_deny = list(data.get("permissions", {}).get("deny", []))
    agents = {
        name: Agent(a["model"], a.get("max_turns", 100), list(a.get("tools", [])),
                    shared_deny + list(a.get("deny", [])), a.get("light_model", ""))
        for name, a in data.get("agents", {}).items()
    }
    for required in ("planner", "implementer", "reviewer"):
        if required not in agents:
            raise ValueError(f"pipeline.toml: missing [agents.{required}]")
    targets = {}
    for name, t in data.get("targets", {}).items():
        changes = t.get("changes", "*")
        if changes != "*" and not isinstance(changes, list):
            raise ValueError(f"target {name}: changes must be a list or \"*\"")
        targets[name] = Target(
            name=name,
            repo_root=Path(os.path.expanduser(t["repo_root"])),
            github_repo=t["github_repo"],
            base=t.get("base", "main"),
            changes_spec=changes,
            project_dir=t.get("project_dir", "."),
            test=t["test"],
            mutation=t.get("mutation", ""),
            context=list(t.get("context", [])),
            work_dir=work_root / name,
            test_timeout=t.get("test_timeout", 3600),
            mutation_timeout=t.get("mutation_timeout", 3600),
            mutation_alongside_gate=bool(t.get("mutation_alongside_gate", False)),
            mutation_covers=t.get("mutation_covers", ""),
            merge_batch=max(1, int(t.get("merge_batch", 1))),
            extra_tools=list(t.get("extra_tools", [])),
            env={k: str(v) for k, v in t.get("env", {}).items()},
            clean_workspace_crates=bool(t.get("clean_workspace_crates", False)),
            cache_max_gb=float(t.get("cache_max_gb", 0)),
            release_branch=t.get("release_branch", ""),
            milestones=[Milestone(m["tag"], m.get("title", m["tag"]), list(m["changes"]),
                                  list(m.get("play", [])), list(m.get("deferred", [])))
                        for m in t.get("milestones", [])],
            release_build=t.get("release_build", ""),
            release_test=t.get("release_test", ""),
            release_assets=list(t.get("release_assets", [])),
            preflight=list(t.get("preflight", [])),
            preflight_gate=bool(t.get("preflight_gate", False)),
            format=t.get("format", ""),
            quick_check=t.get("quick_check", ""),
            front_paths=list(t.get("front_paths", [])),
            front_implementers=int(t.get("front_implementers", 0)),
            dev_gate=t.get("dev_gate", ""),
            dev_gate_minutes=int(t.get("dev_gate_minutes", 60)),
            dev_gate_note=t.get("dev_gate_note", ""),
            overlap_ignore=list(t.get("overlap_ignore", [])),
            parked=list(t.get("parked", [])),
            priority=list(t.get("priority", [])),
            exclusive=list(t.get("exclusive", [])),
            archive=t.get("archive", "openspec archive {change} --yes"),
            auto_approve=bool(t.get("auto_approve", False)),
            watch_promote_at=int(t.get("watch_promote_at", 3)),
            unblocking_slots=int(t.get("unblocking_slots", 1)),
            max_implementers=t.get("max_implementers"),
            max_reviewers=t.get("max_reviewers"),
        )
        tg = targets[name]
        if tg.milestones and not tg.release_branch:
            raise ValueError(f"target {name}: milestones need release_branch")
        if tg.release_branch and tg.release_branch == tg.base:
            raise ValueError(f"target {name}: release_branch must differ from base ({tg.base})")
    return Config(
        targets=targets,
        claude_bin=os.path.expanduser(data.get("claude_bin", "claude")),
        gh_bin=os.path.expanduser(data.get("gh_bin", "gh")),
        agents=agents,
        max_implementers=run.get("max_implementers", 2),
        max_reviewers=run.get("max_reviewers", 1),
        max_rounds=run.get("max_rounds", 3),
        poll_seconds=run.get("poll_seconds", 60),
        sync_seconds=run.get("sync_seconds", 600),
        limit_backoff_seconds=run.get("limit_backoff_seconds", 1800),
        throttle={k: float(v) for k, v in run.get("throttle", {"five_hour": 0.85, "seven_day": 0.93}).items()},
        agent_timeout=run.get("agent_timeout", 4 * 3600),
        resume_within_seconds=run.get("resume_within_seconds", 3000),
        max_batch=run.get("max_batch", 4),
        mcp_servers=dict(data.get("mcp_servers", {})),
        env_guard=bool(data.get("permissions", {}).get("env_guard", True)),
        machine=machine,
        machine_targets=machine_targets,
    )
