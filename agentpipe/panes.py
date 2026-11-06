"""agentpipe watch, pane view: one bordered, scrollable pane per running job, like tmux.

Same data sources as ``watch.render`` (pipeline.log and the agents' live transcripts), but each
job keeps its own pane and scrollback, and transcripts are read incrementally rather than in full
on every refresh. Keys: tab/shift+tab move between panes, m maximizes the focused pane (m or
escape restores it), q quits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.widgets import Footer, RichLog, Static

from agentpipe import watch as w

_STYLE = {"tool": "cyan", "text": "", "error": "red", "denied": "yellow", "done": "green", "step": "magenta"}


def columns_for(n: int) -> int:
    """Grid columns for ``n`` panes: 1, 2, 2x2, 3x2, 3x3, then 4 wide."""
    return 1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4


class Tail:
    """Reads the complete lines appended to a file since the last call."""

    def __init__(self, path: Path, start_at_end: bool = False):
        self.path = path
        self.offset = path.stat().st_size if start_at_end and path.exists() else 0

    def read(self) -> list[str]:
        try:
            with self.path.open("rb") as f:
                if f.seek(0, 2) < self.offset:
                    self.offset = 0  # truncated or replaced
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1  # hold back a half-written last line
        self.offset += end
        return data[:end].decode("utf-8", errors="replace").splitlines()


@dataclass
class PaneState:
    tail: Tail | None = None
    step: str = ""
    stats: w.Transcript = field(default_factory=w.Transcript)


class JobPane(RichLog):
    can_focus = True

    def __init__(self, job: w.Job):
        super().__init__(wrap=True, markup=False, highlight=False, max_lines=3000, auto_scroll=True,
                         classes="job")
        self.job = job
        self.state = PaneState()
        self.border_title = escape(f"[{job.slot}] {job.kind} {job.key} #{job.issue}"
                                   + (f"  {job.about}" if job.about else ""))

    def event(self, when: str, kind: str, text: str) -> None:
        line = Text(f"{when} ", style="dim")
        line.append(text if kind == "text" else w._short(text, 600), style=_STYLE.get(kind, ""))
        self.write(line)


class PanesApp(App):
    TITLE = "agentpipe"
    CSS = """
    #head { height: auto; max-height: 6; padding: 0 1; background: $boost; }
    #jobs { grid-gutter: 0 1; height: 1fr; }
    #idle { height: 1fr; content-align: center middle; color: $text-muted; }
    .job { border: round $primary; scrollbar-size-vertical: 1; }
    .job:focus { border: round $accent; }
    .job.stalled { border: round $error; }
    #log { height: 9; border: round $secondary; scrollbar-size-vertical: 1; }
    """
    BINDINGS = [
        Binding("m", "toggle_max", "maximize pane"),
        Binding("escape", "restore", "restore", show=False),
        Binding("tab", "focus_next", "next pane", show=False),
        Binding("shift+tab", "focus_previous", "previous pane", show=False),
    ]

    def __init__(self, target: str, work_dir: Path, project_dir: str, interval: float = 2.0,
                 linked: bool = False):
        super().__init__()
        # Closing a linked watcher drains the dispatcher; say so where q is shown.
        self.bind("q", "quit", description="quit + stop dispatcher" if linked else "quit")
        self.target, self.work_dir, self.project_dir, self.interval = target, work_dir, project_dir, interval
        self.panes: dict[str, JobPane] = {}  # job key -> pane
        self.log_tail = Tail(work_dir / "logs" / "pipeline.log")

    def compose(self) -> ComposeResult:
        yield Static(id="head")
        yield Grid(Static("no running jobs", id="idle"), id="jobs")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=2000, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"agentpipe {self.target}"
        log = self.query_one("#log", RichLog)
        log.border_title = "dispatcher log"
        for line in self.log_tail.read()[-200:]:
            log.write(line)
        self.tick()
        self.set_interval(self.interval, self.tick)

    # ------------------------------------------------------------ refresh
    def tick(self) -> None:
        now = time.time()
        log = self.query_one("#log", RichLog)
        for line in self.log_tail.read():
            log.write(line)
        path = self.work_dir / "logs" / "pipeline.log"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        st = w.parse_log(lines)
        self.sync_panes(st.jobs)
        for pane in self.panes.values():
            self.update_pane(pane, now)
        quiet = f", quiet for {w._ago(now - st.last_time)}" if st.last_time else ""
        head = [f"Dispatcher: {w._dispatcher_state(self.work_dir)}{quiet}  ·  {len(st.jobs)} running"]
        head += w._state_notes(self.work_dir, now)
        self.query_one("#head", Static).update("\n".join(head))

    def sync_panes(self, jobs: list[w.Job]) -> None:
        grid = self.query_one("#jobs", Grid)
        live = {j.key: j for j in jobs}
        for key in [k for k, p in self.panes.items() if k not in live or live[k].started != p.job.started]:
            self.panes.pop(key).remove()
        for job in sorted(jobs, key=lambda j: j.slot):
            if job.key not in self.panes:
                self.panes[job.key] = pane = JobPane(job)
                grid.mount(pane)
            self.panes[job.key].job = job  # phase updates
        self.query_one("#idle").display = not self.panes
        grid.styles.grid_size_columns = columns_for(len(self.panes))

    def update_pane(self, pane: JobPane, now: float) -> None:
        job, s = pane.job, pane.state
        cwd = w.job_cwd(self.work_dir, self.project_dir, job)
        in_agent = not job.phase or job.phase.startswith("agent")
        path = w.live_transcript(cwd, job.started) if in_agent else None
        if path is not None:
            if s.tail is None or s.tail.path != path:
                s.tail, s.stats = Tail(path), w.Transcript()
                pane.event(time.strftime("%H:%M:%S"), "step", f"agent transcript {path.name}")
            tr = w.parse_transcript(s.tail.read())
            s.stats.tool_calls += tr.tool_calls
            s.stats.errors += tr.errors
            s.stats.denied += tr.denied
            for ev in tr.events:
                pane.event(ev.when, ev.kind, ev.text)
            active = path.stat().st_mtime
            denied = f", {s.stats.denied} denied" if s.stats.denied else ""
            what = f"agent, {s.stats.tool_calls} tools, {s.stats.errors} errors{denied}"
        else:
            step, progress, active = w.job_step(self.work_dir, cwd, job)
            what = step + progress
            if step != s.step:
                s.step = step
                pane.event(time.strftime("%H:%M:%S"), "step", step)
        idle = now - active if active else None
        stalled = idle is not None and idle >= w.STALL_AFTER
        pane.set_class(stalled, "stalled")
        seen = "no activity yet" if idle is None else f"active {w._ago(idle)} ago" + (" STALLED?" if stalled else "")
        pane.border_subtitle = escape(f"{w._ago(now - job.started)} · {what} · {seen}")

    # ------------------------------------------------------------ actions
    def action_toggle_max(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif isinstance(self.focused, (JobPane, RichLog)):
            self.screen.maximize(self.focused)

    def action_restore(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()


def run(target: str, work_dir: Path, project_dir: str, interval: float = 2.0, linked: bool = False) -> None:
    PanesApp(target, work_dir, project_dir, interval, linked).run()
