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
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.widgets import Footer, RichLog, Static

from agentpipe import watch as w
from agentpipe.status import TICKET_CHANGE

def styles_for(theme) -> dict[str, str]:
    """Line styles from the theme's own colours. Fixed ANSI names (magenta, cyan) come out dark
    and unreadable in the Windows console (Max, 2026-11-07)."""
    def c(value, fallback):
        return str(value) if value else fallback

    return {"tool": c(theme.primary, "cyan"), "text": "bold " + c(theme.foreground, "white"),
            "think": "italic " + c(theme.secondary, "blue"),
            "error": c(theme.error, "red"), "denied": c(theme.warning, "yellow"),
            "done": c(theme.success, "green"), "step": "bold " + c(theme.accent, "yellow"),
            "dim": c(theme.primary, "cyan"),  # folded tool calls: the tool colour, not grey
            "missed": "bold " + c(theme.error, "red"), "timeout": c(theme.warning, "yellow")}


# A marker per kind, so thinking, messages and tool calls tell apart without colour too
# (Max, 2026-11-09: it was "just a big grey blob").
MARKS = {"think": "~", "text": ">", "tool": "$", "dim": "$", "error": "!", "denied": "!",
         "done": "+", "step": "#", "missed": "!", "timeout": "!"}
# Prose blocks get a blank line before them, unless the line before is the same kind.
BLOCKS = ("think", "text", "step")


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
    mutants_seen: int = 0


MAX_ENTRIES = 3000
LOG_HISTORY = 1000  # dispatcher log lines loaded when the watcher opens
QUIET = ("tool", "error", "denied")  # folded into one summary line unless tool detail is on


class JobPane(RichLog):
    """One job's stream. By default it shows the agent's reasoning and messages in full and folds
    each run of tool calls (with their errors) into one summary line; clicking that line opens or
    closes it, and ``detail`` shows every call. Entries are kept so a resize, a detail toggle or
    a click can re-wrap them."""

    can_focus = True

    def __init__(self, job: w.Job, detail: bool = False):
        super().__init__(wrap=True, markup=False, highlight=False, max_lines=MAX_ENTRIES * 2,
                         auto_scroll=True, min_width=1, classes="stream")
        self.job = job
        self.state = PaneState()
        self.detail = detail
        self.entries: list[tuple[int, w.Event]] = []  # (sequence number, event)
        self.seq = 0
        self.group: list[w.Event] = []  # quiet events since the last loud one, not yet summarized
        self.group_id = 0  # sequence number of the group's first event: names it for `expanded`
        self.expanded: set[int] = set()  # groups opened by a click
        self.owners: list[int | None] = []  # per drawn line, the group it belongs to (clickable)
        self.last_kind = ""  # kind of the last line written, for block spacing

    def event(self, when: str, kind: str, text: str) -> None:
        ev = w.Event(when, kind, text)
        self.seq += 1
        self.entries.append((self.seq, ev))
        if len(self.entries) > MAX_ENTRIES:
            del self.entries[: len(self.entries) - MAX_ENTRIES]
        self._show(self.seq, ev)

    def _show(self, n: int, ev: w.Event) -> None:
        if ev.kind in QUIET:
            if not self.group:
                self.group_id = n
            self.group.append(ev)
            if self.detail:
                self._write(ev.when, ev.kind, w._short(ev.text, 600))
            return
        if self.group and not self.detail:
            self._flush_group()
        self.group = []
        self._write(ev.when, ev.kind, ev.text)

    def _flush_group(self) -> None:
        """The group's summary line; an opened group also lists each call under it."""
        gid, opened = self.group_id, self.group_id in self.expanded
        self._write(self.group[0].when, "dim", summarize(self.group) + (" [-]" if opened else " [+]"), gid)
        if opened:
            for e in self.group:
                self._write(e.when, e.kind, w._short(e.text, 2000), gid)

    def _write(self, when: str, kind: str, text: str, owner: int | None = None) -> None:
        # Before the pane has a size RichLog only queues writes; on_resize redraws everything
        # then, so skip them here and keep `owners` in step with the lines actually drawn.
        if not self._size_known:
            return
        if kind in BLOCKS and self.last_kind and self.last_kind != kind:
            self._put(Text(""), None)
        self._put(self._line(when, kind, text), owner)
        self.last_kind = kind

    def _put(self, renderable, owner: int | None) -> None:
        # Follow new output only while at the bottom, so reading an opened group isn't yanked away.
        follow = self.scroll_y >= self.max_scroll_y
        before = len(self.lines) + self._start_line  # _start_line counts lines dropped at max_lines
        self.write(renderable, scroll_end=follow)
        self.owners.extend([owner] * (len(self.lines) + self._start_line - before))
        if len(self.owners) > len(self.lines):  # RichLog dropped its oldest lines (max_lines)
            del self.owners[: len(self.owners) - len(self.lines)]

    def on_click(self, event) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        y = offset.y + int(self.scroll_y)
        gid = self.owners[y] if 0 <= y < len(self.owners) else None
        if gid is None:
            return
        self.expanded ^= {gid}
        keep = self.scroll_y
        self.rerender()
        self.scroll_to(y=keep, animate=False, immediate=True)

    def _line(self, when: str, kind: str, text: str) -> Table:
        """Time, marker, then the text with a hanging indent, so wrapped lines stay in its column."""
        style = styles_for(self.app.current_theme).get(kind, "")
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(no_wrap=True, style="dim")
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")
        body = text.strip() if kind in ("text", "think") else text
        grid.add_row(when, Text(MARKS.get(kind, " "), style="bold " + style if style else "bold"),
                     Text(body, style=style, overflow="fold"))
        return grid

    def rerender(self) -> None:
        self.clear()
        self.owners = []
        self.group = []
        self.last_kind = ""
        for n, ev in self.entries:
            self._show(n, ev)

    def now_doing(self) -> str:
        """The latest tool call of the run not yet summarized, for the border."""
        tool = next((e.text for e in reversed(self.group) if e.kind == "tool"), "")
        return w._short(tool, 70)

    def on_resize(self, event) -> None:
        if event.size.width:
            self._size_known = True  # whichever resize handler runs first; nothing is queued
        self.rerender()  # RichLog wraps at write time; re-wrap to the new width


def pane_title(job: w.Job) -> str:
    """'review #344', or 'implement #48 · 4.3' for a task: the slot and the change name mean
    nothing to the watcher, and a ticket's key only repeats its issue number."""
    change, _, ids = job.key.partition("/")
    return f"{job.kind} #{job.issue}" + (f" · {ids}" if ids and change != TICKET_CHANGE else "")


class JobBox(Vertical):
    """A job's bordered pane: its description (up to two lines) above its stream."""

    ALLOW_MAXIMIZE = True  # containers opt out by default

    def __init__(self, pane: JobPane):
        super().__init__(classes="job")
        self.pane = pane
        pane.box = self  # set before mounting finishes, unlike pane.parent
        self.border_title = escape(pane_title(pane.job))

    def compose(self) -> ComposeResult:
        about = Static(self.pane.job.about or self.pane.job.key, classes="about")
        about.tooltip = self.pane.job.about or None
        yield about
        yield self.pane


def summarize(group: list[w.Event]) -> str:
    """'6 tool calls (Bash 4, Read 2), 1 error' for a run of quiet events."""
    names: dict[str, int] = {}
    for e in group:
        if e.kind == "tool":
            name = e.text.split(":", 1)[0]
            names[name] = names.get(name, 0) + 1
    calls = sum(names.values())
    text = f"{calls} tool call{'s' if calls != 1 else ''}"
    if names:
        text += " (" + ", ".join(f"{n} {c}" for n, c in sorted(names.items(), key=lambda x: -x[1])) + ")"
    errors = sum(e.kind == "error" for e in group)
    denied = sum(e.kind == "denied" for e in group)
    if errors:
        text += f", {errors} error{'s' if errors != 1 else ''}"
    if denied:
        text += f", {denied} DENIED"
    return text


class PanesApp(App):
    TITLE = "agentpipe"
    CSS = """
    #head { height: auto; max-height: 6; padding: 0 1; background: $boost; }
    #jobs { grid-gutter: 0 1; height: 1fr; }
    #idle { height: 1fr; content-align: center middle; color: $text-muted; }
    .job { border: round $primary; height: 1fr; }
    .job:focus-within { border: round $accent; }
    .job.stalled { border: round $error; }
    .job .about { height: auto; max-height: 2; text-style: bold; text-overflow: ellipsis; }
    .stream { height: 1fr; scrollbar-size-vertical: 1; overflow-x: hidden; }
    #log { height: 9; border: round $secondary; scrollbar-size-vertical: 1; overflow-x: hidden; }
    #log.-maximized { height: 1fr; }  /* maximizing must give it the screen, not keep 9 rows */
    """
    BINDINGS = [
        Binding("m", "toggle_max", "maximize pane"),
        Binding("t", "toggle_tools", "tool detail"),
        Binding("escape", "restore", "restore", show=False),
        Binding("tab", "focus_next", "next pane", show=False),
        Binding("shift+tab", "focus_previous", "previous pane", show=False),
    ]

    def __init__(self, target: str, work_dir: Path, project_dir: str, interval: float = 2.0,
                 linked: bool = False):
        super().__init__()
        self.linked = linked
        self.seen_dispatcher = False
        # Restart on new agentpipe commits, like the dispatcher: a watcher left on old code
        # misread the new dispatcher's log and showed 6 of 8 jobs (2026-11-09).
        self.code_rev, self.bad_rev, self.last_code_check = w.code_rev(), None, time.time()
        self.restart = False
        # The ANSI themes take colours from the console's palette and come out blank in the
        # classic Windows console (Max, 2026-11-07), so they aren't offered.
        for name in ("ansi-dark", "ansi-light"):
            self.unregister_theme(name)
        self.theme_file = work_dir.parent / "watch-theme"  # shared by every target's watcher
        # Closing a linked watcher drains the dispatcher; say so where q is shown.
        self.bind("q", "quit", description="quit + stop dispatcher" if linked else "quit")
        self.target, self.work_dir, self.project_dir, self.interval = target, work_dir, project_dir, interval
        self.panes: dict[str, JobPane] = {}  # job key -> pane
        self.tool_detail = False
        self.log_tail = Tail(work_dir / "logs" / "pipeline.log")

    def compose(self) -> ComposeResult:
        yield Static(id="head")
        yield Grid(Static("no running jobs", id="idle"), id="jobs")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=LOG_HISTORY * 2,
                      auto_scroll=True, min_width=1)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"agentpipe {self.target}"
        try:
            saved = self.theme_file.read_text(encoding="utf-8").strip()
            if saved in self.available_themes:
                self.theme = saved
        except OSError:
            pass
        self.theme_changed_signal.subscribe(self, self.save_theme)
        log = self.query_one("#log", RichLog)
        log.border_title = "dispatcher log"
        for line in self.log_tail.read()[-LOG_HISTORY:]:  # enough to scroll back through, maximized
            log.write(line)
        self.tick()
        self.set_interval(self.interval, self.tick)

    # ------------------------------------------------------------ refresh
    def tick(self) -> None:
        if self.linked and w.linked_watcher_done(self.work_dir, self):
            self.exit()  # its dispatcher exited; a restart opens a fresh window
            return
        if time.time() - self.last_code_check >= 60:
            self.last_code_check = time.time()
            rev = w.code_rev()
            if rev and self.code_rev and rev != self.code_rev and rev != self.bad_rev:
                if w.code_imports():
                    self.bad_rev = rev
                else:
                    self.restart = True
                    self.exit()  # run() starts the new code in this same window
                    return
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
        waiting = w.awaiting_label(self.work_dir)
        head = [f"Dispatcher: {w._dispatcher_state(self.work_dir)}{quiet}  ·  {len(st.jobs)} running"
                + (f"  ·  {waiting}" if waiting else "")]
        head += w._state_notes(self.work_dir, now)
        self.query_one("#head", Static).update("\n".join(head))

    def sync_panes(self, jobs: list[w.Job]) -> None:
        grid = self.query_one("#jobs", Grid)
        live = {j.key: j for j in jobs}
        for key in [k for k, p in self.panes.items() if k not in live or live[k].started != p.job.started]:
            self.panes.pop(key).box.remove()
        for job in sorted(jobs, key=lambda j: j.slot):
            if job.key not in self.panes:
                self.panes[job.key] = pane = JobPane(job, self.tool_detail)
                grid.mount(JobBox(pane))
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
            # Narrow panes cut the subtitle short: what the agent is doing now goes first.
            what = pane.now_doing() or f"{s.stats.tool_calls} tools"
            if s.stats.errors:
                what += f" · {s.stats.errors} err"
            if s.stats.denied:
                what += f" · {s.stats.denied} DENIED"
        else:
            step, progress, active = w.job_step(self.work_dir, cwd, job)
            what = step + progress
            if step != s.step:
                s.step = step
                pane.event(time.strftime("%H:%M:%S"), "step", step)
            if step == "mutation testing":
                self.mutation_events(pane, cwd, progress)
        idle = now - active if active else None
        stalled = idle is not None and idle >= w.STALL_AFTER
        box = pane.box
        box.set_class(stalled, "stalled")
        # Recent activity is the normal case and goes unsaid; only a quiet minute is news.
        seen = ("STALLED? " if stalled else "") + f"quiet {w._ago(idle)}" if idle is not None and idle >= 60 else ""
        box.border_subtitle = escape(" · ".join(x for x in (w._ago(now - job.started), seen, what) if x))

    @staticmethod
    def mutation_events(pane: JobPane, cwd: Path, progress: str) -> None:
        """Missed and timed-out mutants as they finish, and a progress line every 10."""
        outcomes = w.mutation_outcomes(cwd)
        s, when = pane.state, time.strftime("%H:%M:%S")
        if len(outcomes) <= s.mutants_seen:
            return
        for summary, name in outcomes[s.mutants_seen:]:
            if summary == "MissedMutant":
                pane.event(when, "missed", f"MISSED {name}")
            elif summary == "Timeout":
                pane.event(when, "timeout", f"TIMEOUT {name}")
        if len(outcomes) // 10 > s.mutants_seen // 10 or s.mutants_seen == 0:
            pane.event(when, "dim", progress.strip())
        s.mutants_seen = len(outcomes)

    def save_theme(self, theme) -> None:
        for pane in self.panes.values():
            pane.rerender()  # line colours come from the theme
        try:
            self.theme_file.write_text(theme.name, encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ actions
    def action_toggle_tools(self) -> None:
        self.tool_detail = not self.tool_detail
        for pane in self.panes.values():
            pane.detail = self.tool_detail
            pane.rerender()
        self.notify("tool detail on" if self.tool_detail else "reasoning only; tool calls folded")

    def action_toggle_max(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif isinstance(self.focused, JobPane):
            self.screen.maximize(self.focused.box)  # keep its description and border
        elif isinstance(self.focused, RichLog):
            self.screen.maximize(self.focused)

    def action_restore(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()


def run(target: str, work_dir: Path, project_dir: str, interval: float = 2.0, linked: bool = False) -> None:
    while True:
        app = PanesApp(target, work_dir, project_dir, interval, linked)
        app.run()
        if not app.restart or w.start_successor(work_dir, linked):
            return
        # The new code didn't come up: carry on here rather than leave the window (and, for a
        # linked watcher, the dispatcher) without a watcher.
