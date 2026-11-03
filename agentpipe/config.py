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
    agent_timeout: int = 4 * 3600
    extra: dict = field(default_factory=dict)


def load(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    run = data.get("run", {})
    work_root = Path(os.path.expanduser(data.get("work_root", "E:/Source/.pipeline")))
    agents = {
        name: Agent(a["model"], a.get("max_turns", 100), list(a.get("tools", [])))
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
        )
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
        agent_timeout=run.get("agent_timeout", 4 * 3600),
    )
