"""Load pipeline.toml: shared settings plus one [targets.<name>] table per project."""

from __future__ import annotations

import os
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


@dataclass
class Milestone:
    tag: str  # git tag and GitHub release name, e.g. "step-1-watchable-combat"
    title: str
    changes: list[str]  # OpenSpec changes that must all be ticked off
    # Task keys where the owner plays this release: they wait for its tag, and the release
    # doesn't wait for them (a task inside the milestone would otherwise deadlock it).
    play: list[str] = field(default_factory=list)


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
    # Appended to the implementer's and reviewer's allowed tools for this target only.
    extra_tools: list[str] = field(default_factory=list)
    # Extra environment variables for this target's agents, gate and mutation runs.
    env: dict[str, str] = field(default_factory=dict)
    # Slots reuse one cargo target dir across branches; cargo can then take another branch's
    # artifact for a same-path workspace crate as fresh. True cleans the workspace's own
    # crates (not dependencies) from the slot's target before each gate and agent run.
    clean_workspace_crates: bool = False
    # Milestone releases: when every change of the next milestone is done on `base`,
    # merge it into `release_branch`, tag it and publish a GitHub release.
    release_branch: str = ""
    milestones: list[Milestone] = field(default_factory=list)
    release_build: str = ""  # optional command run in the merged checkout before tagging
    release_assets: list[str] = field(default_factory=list)  # globs (project-relative) to attach
    # Checked before any agent runs (dispatcher start, then each sync while failing):
    # each command must exit 0 from the dispatcher's shell and from an agent's Bash tool.
    preflight: list[str] = field(default_factory=list)
    preflight_gate: bool = False  # also run `test` on the base branch

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
        return list(self.changes_spec)


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
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The preflight agent only runs the targets' check commands, so a small model will
        # do; it gets the implementer's tools and denials, since those are what it checks.
        impl = self.agents.get("implementer")
        pre = self.agents.get("preflight")
        if impl and pre is None:
            self.agents["preflight"] = Agent("claude-haiku-5-5", 20, list(impl.tools), list(impl.deny))
        elif impl and pre and not pre.tools:
            pre.tools = list(impl.tools)


def load(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    run = data.get("run", {})
    work_root = Path(os.path.expanduser(data.get("work_root", "E:/Source/.pipeline")))
    shared_deny = list(data.get("permissions", {}).get("deny", []))
    agents = {
        name: Agent(a["model"], a.get("max_turns", 100), list(a.get("tools", [])),
                    shared_deny + list(a.get("deny", [])))
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
            extra_tools=list(t.get("extra_tools", [])),
            env={k: str(v) for k, v in t.get("env", {}).items()},
            clean_workspace_crates=bool(t.get("clean_workspace_crates", False)),
            release_branch=t.get("release_branch", ""),
            milestones=[Milestone(m["tag"], m.get("title", m["tag"]), list(m["changes"]),
                                  list(m.get("play", [])))
                        for m in t.get("milestones", [])],
            release_build=t.get("release_build", ""),
            release_assets=list(t.get("release_assets", [])),
            preflight=list(t.get("preflight", [])),
            preflight_gate=bool(t.get("preflight_gate", False)),
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
    )
