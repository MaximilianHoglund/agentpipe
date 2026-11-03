"""agentpipe watch: a live view of what the dispatcher and its headless agents are doing.

It reads only files that already exist while a run is going, so it works against a
dispatcher started before it, without a restart:

- ``<work_dir>/logs/pipeline.log`` says which jobs are running (``start`` lines without
  a matching ``done`` or ``crashed``) and what the dispatcher did last.
- Claude Code writes every session, headless ones included, to
  ``~/.claude/projects/<cwd with each non-alphanumeric as '-'>/<session>.jsonl`` as it
  goes. A job's cwd is its worktree, so its newest transcript there is the live one.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[[^\]]+\] (.*)$")
_START_RE = re.compile(r"^start (\w+) (\S+) \(#(\d+)\) on (\S+)$")
_PHASE_RE = re.compile(r"^phase (\S+): (.*)$")
_END_RE = re.compile(r"^(?:done (\w+) (\S+)|(\w+) (\S+) crashed: .*)$")


@dataclass
class Job:
    kind: str
    key: str
    issue: int
    slot: str
    started: float  # epoch seconds
    phase: str = ""  # latest "phase <key>: ..." line, e.g. "mutation testing"
    phase_at: float = 0.0


@dataclass
class LogState:
    jobs: list[Job]
    recent: list[str]  # last dispatcher lines, newest last
    last_time: float | None


def parse_log(lines: list[str], recent: int = 6) -> LogState:
    """Running jobs and the latest activity, from pipeline.log lines."""
    jobs: dict[str, Job] = {}
    last_time = None
    tail: list[str] = []
    for raw in lines:
        m = _LINE_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        stamp, msg = m.groups()
        last_time = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
        tail.append(f"{stamp[11:]} {msg}")
        if msg.startswith("running;") or msg.startswith("running (pid"):
            jobs.clear()  # a (re)started dispatcher has no jobs yet
        elif s := _START_RE.match(msg):
            kind, key, issue, slot = s.groups()
            jobs[key] = Job(kind, key, int(issue), slot, last_time)
        elif (ph := _PHASE_RE.match(msg)) and ph.group(1) in jobs:
            jobs[ph.group(1)].phase, jobs[ph.group(1)].phase_at = ph.group(2), last_time
        elif e := _END_RE.match(msg):
            jobs.pop(e.group(2) or e.group(4), None)
    return LogState(list(jobs.values()), tail[-recent:], last_time)


def project_folder(cwd: Path) -> Path:
    """Where Claude Code keeps transcripts for sessions started in ``cwd``."""
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def job_cwd(work_dir: Path, project_dir: str, job: Job) -> Path:
    slug = job.key.replace("/", "-").replace(".", "_")
    root = "review" if job.kind == "review" else "wt"
    path = work_dir / root / slug
    return path if project_dir in ("", ".") else path / project_dir


def live_transcript(cwd: Path, since: float) -> Path | None:
    """The newest transcript for ``cwd`` written since the job started."""
    folder = project_folder(cwd)
    if not folder.is_dir():
        return None
    files = [p for p in folder.glob("*.jsonl") if p.stat().st_mtime >= since - 5]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


@dataclass
class Event:
    when: str  # HH:MM:SS, local
    kind: str  # tool, text, error, done
    text: str


@dataclass
class Transcript:
    events: list[Event] = field(default_factory=list)
    tool_calls: int = 0
    errors: int = 0
    denied: int = 0


def _short(value, width: int) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= width else s[: width - 1] + "…"


def _tool_summary(name: str, inp: dict) -> str:
    for k in ("command", "file_path", "pattern", "path", "description", "prompt"):
        if k in inp:
            v = inp[k]
            if k == "file_path":
                v = Path(v).name if len(str(v)) > 60 else v
            return f"{name}: {v}"
    return f"{name}: {json.dumps(inp)}" if inp else name


def _local(ts: str) -> str:
    try:
        t = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")  # transcripts stamp UTC
        return time.strftime("%H:%M:%S", time.localtime(calendar.timegm(t)))
    except (ValueError, TypeError):
        return "--:--:--"


_DENIED_RE = re.compile(r"^Permission to use \w+ (?:with command .* )?has been denied")


def _last_tool(tr: Transcript) -> str:
    return next((e.text for e in reversed(tr.events) if e.kind == "tool"), "a tool call")


def parse_transcript(lines: list[str]) -> Transcript:
    tr = Transcript()
    for raw in lines:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue  # a line still being written
        content = (d.get("message") or {}).get("content")
        if d.get("type") not in ("assistant", "user") or not isinstance(content, list):
            continue
        when = _local(d.get("timestamp", ""))
        for c in content:
            t = c.get("type")
            if t == "tool_use":
                tr.tool_calls += 1
                if c.get("name") == "StructuredOutput":
                    tr.events.append(Event(when, "done", "final answer submitted"))
                else:
                    tr.events.append(Event(when, "tool", _tool_summary(c.get("name", "?"), c.get("input") or {})))
            elif t == "text" and c.get("text", "").strip() and d["type"] == "assistant":
                tr.events.append(Event(when, "text", c["text"]))
            elif t == "tool_result" and c.get("is_error"):
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                if _DENIED_RE.search(str(body)):
                    tr.denied += 1
                    tr.events.append(Event(when, "denied", "DENIED " + _last_tool(tr)))
                else:
                    tr.errors += 1
                    tr.events.append(Event(when, "error", str(body)))
    return tr


# ---------------------------------------------------------------- rendering
_COLORS = {"tool": "36", "text": "0", "error": "31", "denied": "33", "done": "32", "dim": "2", "head": "1"}


def _c(kind: str, s: str, color: bool) -> str:
    return f"\x1b[{_COLORS[kind]}m{s}\x1b[0m" if color else s


def _ago(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _dispatcher_state(work_dir: Path) -> str:
    from agentpipe import service
    pid = service.running_pid(work_dir)
    if pid is None:
        return "NOT RUNNING (start: agentpipe start <target>; crashes: logs/dispatcher.out)"
    return f"stopping, pid {pid}" if (work_dir / "stop").exists() else f"running, pid {pid}"


def _state_notes(work_dir: Path, now: float) -> list[str]:
    """Progress the log doesn't show: a usage-limit pause, and blocked-by linking."""
    notes = []
    try:
        state = json.loads((work_dir / "state.json").read_text(encoding="utf-8"))
        planned = len(json.loads((work_dir / "plan.json").read_text(encoding="utf-8"))["tasks"])
    except (OSError, ValueError, KeyError):
        return notes  # missing, or caught mid-write
    if state.get("paused_until", 0) > now:
        notes.append(f"PAUSED (usage limit) until {time.strftime('%H:%M', time.localtime(state['paused_until']))}")
    linked = len(state.get("links", {}))
    if linked < planned:
        notes.append(f"blocked-by links synced for {linked} of {planned} tasks; "
                     "agents start once the first sync finishes")
    return notes


def render(target: str, work_dir: Path, project_dir: str, *, events: int = 8,
           width: int = 120, color: bool = True, now: float | None = None) -> str:
    now = now or time.time()
    log = work_dir / "logs" / "pipeline.log"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    st = parse_log(lines)
    out = [_c("head", f"agentpipe watch {target} - {time.strftime('%H:%M:%S', time.localtime(now))}", color)]
    quiet = f", quiet for {_ago(now - st.last_time)}" if st.last_time else ""
    out.append(_c("head", f"\nDispatcher: {_dispatcher_state(work_dir)} (pipeline.log{quiet})", color))
    out += [f"  {_short(line, width - 2)}" for line in st.recent] or ["  (no log yet)"]
    out += [f"  {line}" for line in _state_notes(work_dir, now)]

    out.append(_c("head", f"\nRunning jobs: {len(st.jobs)}", color))
    if not st.jobs:
        out.append("  none: waiting on sync, GitHub polling, or nothing is ready")
    for job in sorted(st.jobs, key=lambda j: j.slot):
        cwd = job_cwd(work_dir, project_dir, job)
        path = live_transcript(cwd, job.started)
        head = f"[{job.slot}] {job.kind} {job.key} (#{job.issue}), {_ago(now - job.started)}"
        if path is None or (job.phase and not job.phase.startswith("agent")):
            # Not in an agent (yet): say which step, how far along, and when it last did anything.
            what, progress, active = job_step(work_dir, cwd, job)
            out.append(_c("head", f"\n{head}: {what}{progress}", color))
            out.append("  " + _activity(now, active, color))
            continue
        tr = parse_transcript(path.read_text(encoding="utf-8", errors="replace").splitlines())
        denied = f", {tr.denied} DENIED" if tr.denied else ""
        out.append(_c("head", f"\n{head}: agent, {tr.tool_calls} tool calls, {tr.errors} errors{denied}", color))
        out.append("  " + _activity(now, path.stat().st_mtime, color, tr))
        out.append(_c("dim", f"  {path}", color))
        for ev in tr.events[-events:]:
            out.append(f"  {ev.when} " + _c(ev.kind, _short(ev.text, width - 12), color))
    return "\n".join(out)


STALL_AFTER = 900  # seconds without any sign of life before a job is flagged


def _newest_mtime(*paths: Path) -> float | None:
    """Newest modification time among ``paths`` and, for directories, their direct children."""
    best = None
    for p in paths:
        try:
            items = [p, *p.iterdir()] if p.is_dir() else [p]
        except OSError:
            continue
        for q in items:
            try:
                m = q.stat().st_mtime
            except OSError:
                continue
            best = m if best is None or m > best else best
    return best


def mutation_progress(cwd: Path, since: float) -> tuple[str, float | None] | None:
    """(" 78/79 mutants, 3 missed", last activity) from cargo-mutants' output, if it's running."""
    out = cwd / "mutants.out"
    active = _newest_mtime(out, out / "log")
    if active is None or active < since:
        return None
    counts = {}
    for name in ("caught", "missed", "timeout", "unviable"):
        try:
            counts[name] = len((out / f"{name}.txt").read_text(encoding="utf-8").splitlines())
        except OSError:
            counts[name] = 0
    try:
        total = json.loads((out / "outcomes.json").read_text(encoding="utf-8")).get("total_mutants")
    except (OSError, ValueError):
        total = None
    text = f" {sum(counts.values())}/{total or '?'} mutants, {counts['missed']} missed"
    if counts["timeout"]:
        text += f", {counts['timeout']} timed out"
    return text, active


def job_step(work_dir: Path, cwd: Path, job: Job) -> tuple[str, str, float | None]:
    """(step, progress, last activity) for a job that is not running an agent right now."""
    target = work_dir / "target" / job.slot
    build = _newest_mtime(target, target / "debug", target / "debug" / "deps")
    if job.kind == "review" and (m := mutation_progress(cwd, job.started)):
        return "mutation testing", m[0], m[1]
    if job.phase:
        return job.phase, "", max(filter(None, (build, job.phase_at)), default=None)
    if job.kind in ("review", "merge"):
        # Old dispatchers log no phases; a review without an agent or mutants is in its gate.
        return "test gate", "", max(filter(None, (build, job.started)), default=None)
    return "setting up (worktree, prompt)", "", job.started


def _activity(now: float, active: float | None, color: bool, tr=None) -> str:
    if active is None:
        return _c("dim", "no activity seen yet", color)
    idle = now - active
    text = f"last activity {_ago(idle)} ago"
    if idle < STALL_AFTER:
        return _c("done", text, color)
    hint = ""
    if tr is not None:
        last = next((e for e in reversed(tr.events) if e.kind == "tool"), None)
        if last:
            hint = f"; a long command may still be running: {_short(last.text, 80)}"
    return _c("error", f"{text}: POSSIBLY STALLED{hint}", color)


def follow(work_dir: Path, project_dir: str, key: str, *, color: bool = True, poll: float = 1.0) -> None:
    """Stream one job's transcript in full as it grows, like tail -f."""
    seen = 0
    path = None
    while True:
        log = work_dir / "logs" / "pipeline.log"
        st = parse_log(log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else [])
        job = next((j for j in st.jobs if j.key == key), None)
        if job:
            p = live_transcript(job_cwd(work_dir, project_dir, job), job.started)
            if p != path and p is not None:
                path, seen = p, 0
                print(_c("head", f"--- {job.kind} {key}: {p}", color))
        if path is not None:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            # Hold back a possibly half-written last line.
            complete = lines if path.read_bytes().endswith(b"\n") else lines[:-1]
            for ev in parse_transcript(complete[seen:]).events:
                print(f"{ev.when} " + _c(ev.kind, ev.text if ev.kind == "text" else _short(ev.text, 400), color))
            seen = len(complete)
        time.sleep(poll)


def watch(target: str, work_dir: Path, project_dir: str, *, interval: float = 2.0,
          events: int = 8, once: bool = False) -> None:
    if os.name == "nt":
        os.system("")  # turn on ANSI escapes in the Windows console
    color = once is False
    while True:
        size = os.get_terminal_size() if not once and os.isatty(1) else os.terminal_size((120, 40))
        text = render(target, work_dir, project_dir, events=events, width=size.columns, color=color)
        if once:
            print(text)
            return
        print("\x1b[H\x1b[2J" + text, flush=True)
        time.sleep(interval)
