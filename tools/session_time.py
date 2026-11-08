"""Where a pipeline agent's wall time went: tool calls (by command), model time, and the slowest calls.

    uv run python tools/session_time.py E:/Source/.pipeline/arpg ticket/474 [--since 2026-11-08T20:25Z]
    uv run python tools/session_time.py path/to/session.jsonl

With a work dir and a task key it reads the task's current session from state.json and finds its
Claude Code transcript under ~/.claude/projects, which is written live, so it works on a job that
is still running. Calls still waiting for a result are listed as running. Times are UTC.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def transcript(work_dir: Path, key: str) -> Path:
    session = json.loads((work_dir / "state.json").read_text(encoding="utf-8"))["tasks"][key]["session"]
    found = list((Path.home() / ".claude" / "projects").glob(f"*/{session}.jsonl"))
    if not found:
        sys.exit(f"no transcript for {key}'s session {session}")
    return found[0]


def command_key(name: str, what: str) -> str:
    if name not in ("Bash", "PowerShell"):
        return name
    words = what.split()
    return f"{name}: {' '.join(words[:3])}" if words else name


def report(path: Path, since: datetime | None) -> None:
    uses, rows, gaps = {}, [], []
    first = last = prev_result = None
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not e.get("timestamp"):
            continue
        t = ts(e["timestamp"])
        if since and t < since:
            continue
        first, last = first or t, t
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_use":
                inp = c.get("input") or {}
                what = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                uses[c["id"]] = (t, c["name"], " ".join(str(what).split())[:140])
                if prev_result:
                    gaps.append((t - prev_result).total_seconds())
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in uses:
                t0, name, what = uses.pop(c["tool_use_id"])
                rows.append(((t - t0).total_seconds(), name, what, t0))
                prev_result = t
    if not first:
        sys.exit("no events in range")
    span = (last - first).total_seconds()
    tools = sum(r[0] for r in rows)
    print(f"{path.name}: {first:%H:%M}-{last:%H:%M} UTC, {span / 60:.1f} min; tools {tools / 60:.1f} min, "
          f"model ~{sum(gaps) / 60:.1f} min, {len(rows)} calls")
    for t0, name, what in uses.values():
        print(f"  running since {t0:%H:%M}: {name}: {what}")
    by = defaultdict(lambda: [0.0, 0])
    for d, name, what, _ in rows:
        k = command_key(name, what)
        by[k][0] += d
        by[k][1] += 1
    print("By command:")
    for k, (d, n) in sorted(by.items(), key=lambda kv: -kv[1][0])[:12]:
        print(f"  {d / 60:6.1f} min {n:4d}x  {k}")
    print("Slowest calls:")
    for d, name, what, t0 in sorted(rows, key=lambda r: -r[0])[:12]:
        print(f"  {d / 60:6.1f} min  {t0:%H:%M}  {name}: {what}")


def main(argv: list[str]) -> None:
    since = None
    if "--since" in argv:
        i = argv.index("--since")
        since = ts(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) == 1:
        report(Path(argv[0]), since)
    elif len(argv) == 2:
        report(transcript(Path(argv[0]).expanduser(), argv[1]), since)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
