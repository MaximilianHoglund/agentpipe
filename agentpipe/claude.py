"""Run headless Claude Code (`claude -p`) with structured output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agentpipe import proc
from agentpipe.config import Agent

# Messages that mean "out of quota / capacity", as opposed to a failed task.
LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|limit (?:reached|exceeded)|resets? at|overloaded|"
    r"low[ -]priority|capacity|\b429\b|\b529\b",
    re.I,
)


@dataclass
class ClaudeResult:
    ok: bool
    session_id: str | None
    text: str
    structured: dict | None
    limited: bool
    cost_usd: float = 0.0
    subtype: str = ""


def parse_output(stdout: str, stderr: str, code: int) -> ClaudeResult:
    data = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if data is None:
        msg = (stderr or stdout or f"exit {code}").strip()
        return ClaudeResult(False, None, msg, None, bool(LIMIT_RE.search(msg)))
    text = str(data.get("result", ""))
    is_error = bool(data.get("is_error")) or data.get("subtype") != "success"
    structured = data.get("structured_output")
    limited = is_error and bool(LIMIT_RE.search(text + " " + stderr))
    return ClaudeResult(
        ok=not is_error and structured is not None,
        session_id=data.get("session_id"),
        text=text,
        structured=structured,
        limited=limited,
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        subtype=str(data.get("subtype", "")),
    )


class Claude:
    def __init__(self, bin: str, log_dir: Path, timeout: int, run=proc.run):
        self.bin = bin
        self.log_dir = log_dir
        self.timeout = timeout
        self._run = run

    def run(self, prompt: str, cwd: Path, agent: Agent, schema: dict, *, log_name: str,
            resume: str | None = None, env=None) -> ClaudeResult:
        cmd = [self.bin, "-p", "--output-format", "json", "--model", agent.model,
               "--permission-mode", "dontAsk", "--max-turns", str(agent.max_turns),
               "--json-schema", json.dumps(schema)]
        if resume:
            cmd += ["--resume", resume]
        if agent.tools:
            cmd += ["--allowedTools", *agent.tools]
        # The prompt goes on stdin: --allowedTools is variadic and would swallow it.
        r = self._run(cmd, cwd, input=prompt, env=env, timeout=self.timeout, check=False)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / f"{log_name}.prompt.md").write_text(prompt, encoding="utf-8")
        (self.log_dir / f"{log_name}.out.json").write_text(r.out + "\n" + r.err, encoding="utf-8")
        return parse_output(r.out, r.err, r.code)
