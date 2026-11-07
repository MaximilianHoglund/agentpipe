"""agentpipe denials: review the tool calls agents were refused.

Every headless run's JSON result lists its ``permission_denials``, and the dispatcher
keeps each result as ``logs/<run>.out.json``, so this reads the whole history with no
extra bookkeeping. Each denial is classed by why it happened:

- ``deny rule``: it matches ``[permissions] deny``. Usually working as intended; a rule
  that keeps firing on harmless work may be too broad.
- ``not allowed``: no allow rule covers the tool. That is a gap: either allow the
  tool or tell the agents to do it another way.
- ``protected file``: an Edit or Write the agent's allowlist permits, refused by Claude
  Code's own safety check on files that can run code (``.cargo/config.toml``, ``.git/``,
  ``.claude/``). No config rule lifts it: steer agents to report the change instead.
- ``no longer denied``: today's config would let it through (a rule was narrowed or a
  tool allowed since), or Claude Code's own checks refused a shell command (protected
  paths in ``Remove-Item``). Mostly history; recent entries are worth a look.

A run killed mid-way leaves no result, so its denials are missing here; watch shows
live ones.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_RULE_RE = re.compile(r"^(\w+)(?:\((.*?)(?::\*)?\))?$")
_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}


@dataclass
class Denial:
    run: str  # log name, e.g. project-foundation-1_1-impl-1
    when: float  # epoch seconds of the run's result
    tool: str
    command: str  # the command, file path or input, as text
    reason: str  # "deny rule <rule>" or "not allowed"


def describe(d: dict, width: int = 200) -> str:
    """'Tool: command' for one raw permission_denials entry."""
    inp = d.get("tool_input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("url") or inp.get("pattern") or ""
    if not detail and inp:
        detail = json.dumps(inp)
    s = " ".join(f"{d.get('tool_name', '?')}: {detail}".split())
    return s if len(s) <= width else s[: width - 1] + "…"


def matching_rule(tool: str, command: str, deny: list[str]) -> str | None:
    """The deny rule this call hits, judged per part of a compound command."""
    parts = [" ".join(p.split()) for p in _SPLIT_RE.split(command)] if command else [""]
    for rule in deny:
        m = _RULE_RE.match(rule.strip())
        if not m or m.group(1) != tool:
            continue
        prefix = " ".join((m.group(2) or "").split())
        if not prefix:
            return rule  # the whole tool is denied
        for p in parts:
            if p == prefix or p.startswith(prefix + " ") or p.lower().startswith(prefix.lower() + " ") \
                    or p.lower() == prefix.lower():
                return rule
    return None


def scan(logs: Path, deny: list[str], since: float = 0.0, allowed: set[str] = frozenset()) -> list[Denial]:
    """``allowed``: tools allowed outright today, e.g. {"Bash", "PowerShell"}."""
    out = []
    for path in sorted(logs.glob("*.out.json")):
        when = path.stat().st_mtime
        if when < since:
            continue
        data = _result(path)
        for d in (data or {}).get("permission_denials") or []:
            tool = d.get("tool_name", "?")
            inp = d.get("tool_input") or {}
            command = str(inp.get("command") or inp.get("file_path") or inp.get("url") or json.dumps(inp))
            rule = matching_rule(tool, command, deny)
            if rule:
                reason = f"deny rule {rule}"
            elif tool not in allowed:
                reason = "not allowed"
            elif tool in _FILE_TOOLS:
                reason = "protected file"
            else:
                reason = "no longer denied"
            out.append(Denial(path.name.removesuffix(".out.json"), when, tool, command, reason))
    return out


def _result(path: Path) -> dict | None:
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _group_key(d: Denial) -> str:
    """Tool plus the first two words of the first command part: 'Bash: cargo --version'."""
    first = _SPLIT_RE.split(d.command)[0].split()
    return f"{d.tool}: {' '.join(first[:2])}" if first else d.tool


_HEADINGS = {
    "not allowed": "NOT ALLOWED (gaps: allow the tool, or steer agents elsewhere)",
    "deny rule": "DENY RULES (intended; check none fire on harmless work)",
    "protected file": "PROTECTED FILES (Claude Code's own check; the change needs a human)",
    "no longer denied": "NO LONGER DENIED (today's config allows these; or a Claude Code path check)",
}


def report(denials: list[Denial], *, examples: int = 2, width: int = 110) -> str:
    if not denials:
        return "No denied tool calls."
    runs = {d.run for d in denials}
    lines = [f"{len(denials)} denied tool call(s) in {len(runs)} run(s).", ""]
    for reason in ("not allowed", "protected file", "deny rule", "no longer denied"):
        group = [d for d in denials if d.reason.startswith(reason)]
        if not group:
            continue
        lines.append(_HEADINGS[reason])
        by: dict[str, list[Denial]] = defaultdict(list)
        for d in group:
            by[d.reason + "\x00" + _group_key(d)].append(d)
        for k, ds in sorted(by.items(), key=lambda kv: -len(kv[1])):
            rule, name = k.split("\x00")
            tag = f"  [{rule.removeprefix('deny rule ')}]" if reason == "deny rule" else ""
            lines.append(f"  {len(ds):3}x  {name}{tag}   runs: {', '.join(sorted({d.run for d in ds}))}")
            for d in ds[:examples]:
                cmd = " ".join(d.command.split())
                lines.append(f"         {time.strftime('%m-%d %H:%M', time.localtime(d.when))}  "
                             f"{cmd if len(cmd) <= width else cmd[: width - 1] + '…'}")
        lines.append("")
    return "\n".join(lines).rstrip()
