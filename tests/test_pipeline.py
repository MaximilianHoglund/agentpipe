"""Scheduler and ticket handling against an in-memory GitHub; jobs are recorded, not run."""

import dataclasses
import os
import time
from pathlib import Path

import pytest

from agentpipe import proc as proc_mod
from agentpipe.config import Agent, Config, Target
from agentpipe.deps import Plan, TaskDeps
from agentpipe.pipeline import Pipeline
from agentpipe.status import TASK_MARKER, TICKET_MARKER, Status
from agentpipe.tasks import parse_tasks
from gitutil import commit_on, git, make_repo

S = Status


class FakeGitHub:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.prs: list[dict] = []
        self.comments_by: dict[int, list[dict]] = {}
        self.n = 0

    def add(self, body, status, title="t", state="OPEN", extra=()):
        self.n += 1
        self.issues[self.n] = {"number": self.n, "state": state, "title": title, "body": body,
                               "labels": ["pipeline", status.label, *extra]}
        return self.n

    def list_issues(self):
        return [dict(i, labels=list(i["labels"])) for i in self.issues.values()]

    def list_prs(self):
        return self.prs

    def create_issue(self, title, body, status, extra_labels=()):
        return self.add(body, status, title, extra=extra_labels)

    def set_status(self, number, new, old):
        labels = [l for l in self.issues[number]["labels"] if not l.startswith("status: ")]
        self.issues[number]["labels"] = labels + [new.label]

    def comment(self, number, body):
        self.comments_by.setdefault(number, []).append({"body": body, "user": "me", "created_at": "now"})

    def comments(self, number):
        return self.comments_by.get(number, [])

    def ensure_labels(self):
        pass


@pytest.fixture
def pl(tmp_path: Path):
    base = tmp_path / "work" / "base"
    (base / "openspec" / "changes" / "c").mkdir(parents=True)
    (base / "openspec" / "changes" / "c" / "tasks.md").write_text("- [ ] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n")
    target = Target("t", tmp_path, "o/r", "main", ["c"], ".", "true", "", [], tmp_path / "work")
    agent = Agent("m", 1, [])
    cfg = Config({"t": target}, "claude", "gh", {"planner": agent, "implementer": agent, "reviewer": agent},
                 max_implementers=2, max_reviewers=1)
    gh = FakeGitHub()
    p = Pipeline(cfg, target, gh=gh, log=lambda m: None)
    tasks = parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n")
    p.tasks = {t.key: t for t in tasks}
    p.plan = Plan("fp", {t.key: TaskDeps() for t in tasks})
    p.last_sync = time.time()
    p.started = []
    p.start = lambda kind, key, slot, issue, pr: (p.started.append((kind, key, slot)),
                                                  p.jobs.__setitem__(key, _Job(kind, slot)))
    return p, gh


class _Job:
    def __init__(self, kind, slot):
        self.kind, self.slot = kind, slot
        self.future = _NotDone()


class _NotDone:
    def done(self):
        return False


def task_issue(gh, key, status):
    return gh.add(TASK_MARKER.format(key=key), status)


def test_fills_implementer_slots_resuming_interrupted_work_first(pl):
    p, gh = pl
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    task_issue(gh, "c/1.3", S.IN_PROGRESS)
    task_issue(gh, "c/1.4", S.BLOCKED_BY_TASK)
    p.tick()
    assert p.started == [("implement", "c/1.3", "impl-0"), ("implement", "c/1.1", "impl-1")]
    p.tick()  # no free slots, nothing new
    assert len(p.started) == 2


def test_reviews_only_unreviewed_heads_and_merges_one_at_a_time(pl):
    p, gh = pl
    task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    task_issue(gh, "c/1.2", S.PENDING_REVIEW)
    task_issue(gh, "c/1.3", S.APPROVED)
    task_issue(gh, "c/1.4", S.APPROVED)
    gh.prs = [{"number": 10 + i, "headRefOid": f"sha{i}", "body": TASK_MARKER.format(key=f"c/1.{i}")}
              for i in range(1, 5)]
    gh.comments_by[11] = [{"body": "<!-- pipeline-review sha=sha1 verdict=request_changes round=1 -->"}]
    p.tick()
    # PR 11's head was already reviewed, so the reviewer takes c/1.2; one merge starts.
    assert ("review", "c/1.2", "review-0") in p.started
    assert ("review", "c/1.1", "review-0") not in p.started
    assert [s for s in p.started if s[0] == "merge"] == [("merge", "c/1.3", "merge")]


def test_review_without_pr_goes_to_needs_decision_but_untouched_tickets_become_proposals(pl):
    p, gh = pl
    n = task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    fresh = gh.add(TICKET_MARKER + "\nfix it", S.PROPOSED, title="Dedupe parser")
    legacy = gh.add(TICKET_MARKER + "\nfix it", S.PENDING_REVIEW, title="Old shared label")
    legacy_label = gh.add(TICKET_MARKER + "\nfix it", S.PENDING_REVIEW, title="Not renamed yet")
    gh.issues[legacy_label]["labels"] = ["pipeline", "status: pending approval"]
    worked = gh.add(TICKET_MARKER + "\nfix it", S.PENDING_REVIEW, title="PR got closed")
    p.state.task(f"ticket/{worked}")["runs"] = 1
    p.state.data["tickets"] = [fresh, legacy, legacy_label, worked]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]
    for t in (fresh, legacy, legacy_label):  # proposals wait for a human
        assert S.PROPOSED.label in gh.issues[t]["labels"]
        assert not any(k == f"ticket/{t}" for _, k, _ in p.started)
    assert S.NEEDS_DECISION.label in gh.issues[worked]["labels"]
    p.tick()  # to_decision shows in the count from the next tick
    assert sorted(p.state.data["awaiting_input"]) == sorted([n, fresh, legacy, legacy_label, worked])


def test_parked_and_deferred_items_are_not_waiting_on_max(pl):
    from agentpipe.config import Milestone
    p, gh = pl
    d = task_issue(gh, "c/1.1", S.NEEDS_DECISION)
    by_key = task_issue(gh, "c/1.2", S.NEEDS_DECISION)
    deferred = task_issue(gh, "c/1.3", S.NEEDS_DECISION)
    by_number = gh.add(TICKET_MARKER + "\nfix it", S.PROPOSED, title="Needs a GPU")
    p.state.data["tickets"] = [by_number]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.t.parked = ["c/1.2", by_number]
    p.t.milestones = [Milestone("step-1", "Step 1", ["c"], deferred=["c/1.3"])]
    p.tick()
    assert p.state.data["awaiting_input"] == [d]
    assert S.NEEDS_DECISION.label in gh.issues[by_key]["labels"]  # parked, not changed
    assert S.NEEDS_DECISION.label in gh.issues[deferred]["labels"]


def test_records_issues_waiting_on_max_for_the_watcher(pl):
    from agentpipe import watch
    p, gh = pl
    assert watch.awaiting_label(p.t.work_dir) == ""  # nothing counted before the first poll
    d = task_issue(gh, "c/1.1", S.NEEDS_DECISION)
    task_issue(gh, "c/1.2", S.PENDING_REVIEW)  # PR open: in review, not his
    gh.prs = [{"number": 20, "headRefOid": "sha", "body": TASK_MARKER.format(key="c/1.2")}]
    task_issue(gh, "c/1.3", S.PENDING_IMPLEMENTATION)
    t = gh.add(TICKET_MARKER + "\nfix it", S.PROPOSED, title="Dedupe parser")
    p.state.data["tickets"] = [t]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    assert p.state.data["awaiting_input"] == sorted([d, t])
    assert watch.awaiting_label(p.t.work_dir) == "2 waiting on you"
    gh.set_status(d, S.PENDING_IMPLEMENTATION, S.NEEDS_DECISION)  # Max answered
    p.tick()
    assert watch.awaiting_label(p.t.work_dir) == "1 waiting on you"
    gh.set_status(t, S.AWAITING_TASK, S.PROPOSED)  # decided, waits for another task
    p.tick()
    assert watch.awaiting_label(p.t.work_dir) == "0 waiting on you"
    assert S.AWAITING_TASK.label in gh.issues[t]["labels"]  # the pipeline leaves it alone
    assert not any(k == f"ticket/{t}" for _, k, _ in p.started)


def test_approved_ticket_is_implemented_with_its_body_as_spec(pl):
    p, gh = pl
    t = gh.add(TICKET_MARKER + "\nThe parser in a.rs duplicates b.rs", S.PENDING_IMPLEMENTATION,
               title="Dedupe parser", extra=("kind: defect",))
    p.state.data["tickets"] = [t]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    assert ("implement", f"ticket/{t}", "impl-0") in p.started
    task = p.tasks[f"ticket/{t}"]
    assert task.section == "defect" and task.branch == f"pipeline/ticket/{t}"
    assert "duplicates b.rs" in p.spec_ref(task) and "<!--" not in p.spec_ref(task)


def test_file_tickets_creates_proposed_issues_and_skips_duplicates(pl):
    p, gh = pl
    p.file_tickets("the review of PR #3", [
        {"kind": "defect", "title": "Off-by-one in pager", "body": "pager.rs:10"},
        {"kind": "bogus", "title": "Unify config loading", "body": "x"},
        {"kind": "defect", "title": "off-by-one in pager ", "body": "dup, different case"},
        {"kind": "defect", "title": "  ", "body": "no title"},
    ])
    created = list(gh.issues.values())
    assert [i["title"] for i in created] == ["Off-by-one in pager", "Unify config loading"]
    assert all(S.PROPOSED.label in i["labels"] for i in created)
    assert "kind: improvement" in created[1]["labels"]  # unknown kind falls back
    assert set(p.state.data["tickets"]) == {i["number"] for i in created}
    # They belong to this target and are offered to later agents as known tickets.
    assert set(p.issues_by_key(gh.list_issues())) == {f"ticket/{i['number']}" for i in created}
    assert "Off-by-one in pager" in p.open_tickets()


def test_issues_of_other_targets_are_ignored(pl):
    p, gh = pl
    task_issue(gh, "other-change/1.1", S.PENDING_IMPLEMENTATION)
    gh.add(TICKET_MARKER + "\nfiled by another target", S.PENDING_IMPLEMENTATION)
    assert p.issues_by_key(gh.list_issues()) == {}
    p.tick()
    assert p.started == []


def test_paused_pipeline_starts_nothing(pl):
    p, gh = pl
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    p.state.data["paused_until"] = time.time() + 60
    p.tick()
    assert p.started == []


def test_first_sync_starts_agents_before_creating_blocked_issues(pl):
    p, gh = pl
    tasks = list(p.tasks.values())
    deps = {"c/1.1": TaskDeps(["c/1.4"]), "c/1.2": TaskDeps(["c/1.4"]),
            "c/1.3": TaskDeps(), "c/1.4": TaskDeps()}
    p.tasks = {}
    p.last_sync = 0
    p.refresh_base = lambda: None
    p.load_tasks = lambda: tasks
    p.make_plan = lambda tasks, fp, old=None: Plan(fp, deps)
    p.sync_links = lambda gh, issues: None
    p.backfill_touches = lambda tasks, plan: False
    seen = []
    p.start = lambda kind, key, slot, issue, pr: (seen.append((key, len(gh.issues))),
                                                  p.jobs.__setitem__(key, _Job(kind, slot)))
    p.tick()
    # Both ready tasks start while only their two issues exist; the blocked ones come after.
    assert seen == [("c/1.3", 2), ("c/1.4", 2)]
    assert len(gh.issues) == 4


def test_state_save_retries_while_the_file_is_held_open(tmp_path, monkeypatch):
    from agentpipe import pipeline as pmod
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(pmod.os, "replace", flaky)
    st = pmod.State(tmp_path / "state.json")
    st.data["paused_until"] = 1.0
    monkeypatch.setattr(pmod.time, "sleep", lambda s: None)
    st.save()
    assert len(calls) == 3
    assert '"paused_until": 1.0' in (tmp_path / "state.json").read_text(encoding="utf-8")


def test_stop_file_drains_running_jobs_then_exits(pl):
    p, gh = pl
    p.cfg.poll_seconds = 0
    lines, ticks = [], []
    p._log = lines.append

    class Done:
        def __init__(self):
            self.n = 0

        def done(self):
            self.n += 1
            return self.n > 2  # still running for two more ticks after the stop request

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            p.jobs["c/1.1"] = type("J", (), {"kind": "implement", "slot": "impl-0", "future": Done()})()
            p.stop_file.write_text("", encoding="utf-8")
        elif p.jobs["c/1.1"].future.done():
            p.jobs.clear()

    p.tick = tick
    p.run_forever()
    assert any("stop requested" in l and "1 running job" in l for l in lines)
    assert lines[-1].endswith("stopped")
    assert not p.stop_file.exists() and not p.pid_file.exists()


def test_removing_the_stop_file_while_draining_cancels_the_stop(pl):
    p, gh = pl
    p.cfg.poll_seconds = 0
    lines, ticks = [], []
    p._log = lines.append
    job = type("J", (), {"kind": "implement", "slot": "impl-0"})()

    def tick():
        ticks.append(1)
        n = len(ticks)
        if n == 1:
            p.jobs["c/1.1"] = job
            p.stop_file.write_text("", encoding="utf-8")
        elif n == 3:
            assert p.stopping
            p.stop_file.unlink()  # `agentpipe start` while it drains
        elif n == 5:
            assert not p.stopping
            p.jobs.clear()  # no jobs, but not stopping: it must keep running
        elif n == 7:
            p.stop_file.write_text("", encoding="utf-8")
        assert n < 12

    p.tick = tick
    p.run_forever()
    assert any(l.endswith("stop cancelled: running again") for l in lines)
    assert len(ticks) >= 7 and lines[-1].endswith("stopped")


def test_linked_watcher_closes_once_its_dispatcher_exits(tmp_path, monkeypatch):
    from agentpipe import service, watch
    pid = {"v": None}
    monkeypatch.setattr(service, "running_pid", lambda wd: pid["v"])
    seen = type("S", (), {"seen_dispatcher": False})()
    assert not watch.linked_watcher_done(tmp_path, seen)  # opened before the dispatcher's pid file
    pid["v"] = 123
    assert not watch.linked_watcher_done(tmp_path, seen)
    pid["v"] = None
    assert watch.linked_watcher_done(tmp_path, seen)


def test_closing_the_linked_watcher_stops_the_dispatcher(pl, monkeypatch):
    from agentpipe import service
    p, gh = pl
    p.cfg.poll_seconds = 0
    lines, ticks = [], []
    p._log = lines.append
    wdir = service.watchers_dir(p.t.work_dir)
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "4242").write_text("", encoding="utf-8")
    alive = {4242: True}
    monkeypatch.setattr(service, "is_alive", lambda pid: alive.get(pid, False))

    def tick():
        ticks.append(1)
        if len(ticks) == 3:
            alive[4242] = False  # the window is closed
        assert len(ticks) < 10

    p.tick = tick
    p.run_forever()
    assert any("watcher window closed: starting nothing new" in l for l in lines)
    assert lines[-1].endswith("stopped")
    assert not (wdir / "4242").exists()


def test_no_linked_watcher_means_no_watcher_stop(pl):
    p, gh = pl
    p.had_watcher = False
    assert p.stop_reason() is None


def test_is_alive_tells_live_from_dead_pids():
    from agentpipe import service
    assert service.is_alive(os.getpid())
    assert not service.is_alive(2 ** 22 + 3)


class LinkingGitHub(FakeGitHub):
    """Blocked-by links that, like GitHub, refuse an add that would close a cycle."""

    def __init__(self):
        super().__init__()
        self.links: dict[int, set[int]] = {}  # issue -> blockers

    def issue_db_id(self, number):
        return 1000 + number

    def blocked_by(self, number):
        return {n: 1000 + n for n in self.links.get(number, set())}

    def _reaches(self, start, goal):
        seen, todo = set(), [start]
        while todo:
            n = todo.pop()
            if n == goal:
                return True
            if n not in seen:
                seen.add(n)
                todo.extend(self.links.get(n, ()))
        return False

    def add_blocked_by(self, number, blocker_db_id):
        from agentpipe.proc import CommandError
        blocker = blocker_db_id - 1000
        if self._reaches(blocker, number):
            raise CommandError("gh api ... exited 1: would create a cycle")
        self.links.setdefault(number, set()).add(blocker)

    def remove_blocked_by(self, number, blocker_db_id):
        self.links[number].discard(blocker_db_id - 1000)

    def blockers(self, number):
        return {n: {"state": self.issues[n]["state"].lower(), "reason": self.issues[n].get("reason")}
                for n in self.links.get(number, set())}

    def blocking(self, number):
        return sorted(w for w, bs in self.links.items() if number in bs)

    def issue(self, number):
        i = self.issues[number]
        return {"number": number, "state": i["state"], "labels": list(i["labels"])}

    def close_issue(self, number, reason="completed"):
        self.issues[number]["state"] = "CLOSED"
        self.issues[number]["reason"] = reason


@pytest.fixture
def linked(pl):
    p, _ = pl
    gh = LinkingGitHub()
    p.gh = gh
    return p, gh


def status_of(gh, n):
    return next(l for l in gh.issues[n]["labels"] if l.startswith("status: "))


def test_a_waiter_is_released_only_when_its_last_blocker_is_done(linked):
    p, gh = linked
    a = task_issue(gh, "c/1.1", S.IN_PROGRESS)
    b = task_issue(gh, "c/1.2", S.IN_PROGRESS)
    w = gh.add(TICKET_MARKER + "\nlater", S.PROPOSED)
    p.register_wait(w, [a, b], S.PROPOSED)
    assert status_of(gh, w) == S.AWAITING_TASK.label
    assert gh.blocking(a) == [w] and gh.blocking(b) == [w]  # each blocker links back to it
    assert any(f"#{w} waits for this" in c["body"] for c in gh.comments(a))
    gh.close_issue(a)
    p.release_waiters(a)
    assert status_of(gh, w) == S.AWAITING_TASK.label  # b is still open
    gh.close_issue(b)
    p.release_waiters(b)
    assert status_of(gh, w) == S.PENDING_IMPLEMENTATION.label
    assert "Released" in gh.comments(w)[-1]["body"]


def test_sweep_releases_waiters_whose_blockers_closed_outside_the_pipeline(linked):
    p, gh = linked
    a = task_issue(gh, "c/1.1", S.IN_PROGRESS)
    w = gh.add(TICKET_MARKER + "\nlater", S.PROPOSED)
    p.register_wait(w, [a], S.PROPOSED)
    gh.close_issue(a)  # e.g. merged by hand while the dispatcher was down
    p.check_waiters({"x": gh.issue(w)})
    assert status_of(gh, w) == S.PENDING_IMPLEMENTATION.label


def test_waiter_on_a_dropped_blocker_or_on_nothing_goes_to_a_human(linked):
    p, gh = linked
    a = task_issue(gh, "c/1.1", S.IN_PROGRESS)
    w = gh.add(TICKET_MARKER + "\nlater", S.PROPOSED)
    p.register_wait(w, [a], S.PROPOSED)
    gh.close_issue(a, "not_planned")  # rejected: what it waited for will never land
    p.release_waiters(a)
    assert status_of(gh, w) == S.NEEDS_DECISION.label
    lone = gh.add(TICKET_MARKER + "\nparked", S.AWAITING_TASK)  # labelled by hand, no links
    p.check_waiters({"x": gh.issue(lone)})
    assert status_of(gh, lone) == S.NEEDS_DECISION.label


def test_approved_ticket_naming_a_task_awaits_it_and_is_released_by_its_merge(linked):
    p, gh = linked
    blocker = task_issue(gh, "c/1.2", S.IN_PROGRESS)
    n = gh.add(TICKET_MARKER + "\nNeeds c/1.2 first.", S.PENDING_IMPLEMENTATION, extra=("kind: defect",))
    p.state.data["tickets"] = [n]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    assert f"ticket/{n}" not in {k for _, k, _ in p.started}
    assert status_of(gh, n) == S.AWAITING_TASK.label and gh.blocking(blocker) == [n]
    gh.close_issue(blocker)
    p.release_waiters(blocker)
    assert status_of(gh, n) == S.PENDING_IMPLEMENTATION.label


def test_a_replan_that_reverses_an_edge_relinks_without_a_cycle(tmp_path):
    from agentpipe.deps import TaskDeps
    change = tmp_path / "work" / "base" / "openspec" / "changes" / "c"
    change.mkdir(parents=True)
    (change / "tasks.md").write_text("- [ ] 1.1 a\n- [ ] 1.2 b\n")
    target = Target("t", tmp_path, "o/r", "main", ["c"], ".", "true", "", [], tmp_path / "work")
    agent = Agent("m", 1, [])
    cfg = Config({"t": target}, "claude", "gh", {"planner": agent, "implementer": agent, "reviewer": agent})
    gh = LinkingGitHub()
    p = Pipeline(cfg, target, gh=gh, log=lambda m: None)
    p.tasks = {t.key: t for t in parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.2 b\n")}
    a = task_issue(gh, "c/1.1", S.BLOCKED_BY_TASK)
    b = task_issue(gh, "c/1.2", S.BLOCKED_BY_TASK)
    issues = p.issues_by_key(gh.list_issues())

    p.plan = Plan("fp", {"c/1.1": TaskDeps(), "c/1.2": TaskDeps(blocked_by=["c/1.1"])})
    p.sync_links(gh, issues)
    assert gh.links == {b: {a}}

    # The new plan reverses the edge; 1.1 is processed first, so a naive add-then-remove cycles.
    p.plan = Plan("fp", {"c/1.1": TaskDeps(blocked_by=["c/1.2"]), "c/1.2": TaskDeps()})
    p.sync_links(gh, issues)
    assert gh.links == {a: {b}, b: set()}


def test_clean_workspace_crates_cleans_only_members(pl, tmp_path, monkeypatch):
    p, _ = pl
    ws = tmp_path / "ws"
    (ws / "a" / "src").mkdir(parents=True)
    (ws / "a" / "src" / "lib.rs").write_text("")
    (ws / "Cargo.toml").write_text('[workspace]\nmembers = ["a"]\nresolver = "2"\n')
    (ws / "a" / "Cargo.toml").write_text('[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n')
    calls = []
    real = proc_mod.run

    def spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(proc_mod, "run", spy)
    p.clean_workspace_crates(ws, "impl-0")
    assert calls == []  # off by default
    p.t.clean_workspace_crates = True
    p.clean_workspace_crates(ws, "impl-0")
    assert calls[-1] == ["cargo", "clean", "-p", "a"]
    calls.clear()
    p.clean_workspace_crates(tmp_path, "impl-0")  # no Cargo.toml: nothing to do
    assert calls == []


def test_mutation_run_does_not_clean_what_the_gate_just_built(pl, tmp_path, monkeypatch):
    from agentpipe import claude as claude_mod
    from agentpipe.claude import ClaudeResult
    p, gh = pl
    p.t.mutation = "mutants --in-diff {diff}"
    p.t.clean_workspace_crates = True
    rv = tmp_path / "rv"
    rv.mkdir()
    cleaned, ran = [], []
    monkeypatch.setattr(p, "clean_workspace_crates", lambda cwd, slot: cleaned.append(slot))
    class FakeBackground:
        def __init__(self, cmd, *a, **kw):
            ran.append(cmd)

        def result(self):
            return proc_mod.Result(0, "ok", "")

    monkeypatch.setattr(proc_mod, "Background", FakeBackground)
    p.git = type("G", (), {"diff": lambda self, *a, **kw: "", "restore": lambda self, *a: None})()
    verdict = {"verdict": "approve", "summary": "s", "findings": [], "probes": [], "tickets": []}
    p.claude = type("C", (), {"run": lambda self, *a, **kw: ClaudeResult(
        True, "", "", verdict, False, subtype="success")})()
    t = p.tasks["c/1.1"]
    pr = {"number": 7, "headRefOid": "abc1234567", "body": "## Summary\nx"}
    p.reviewer_verdict(t, pr, rv, rv, "review-0", [])
    assert any(str(c).startswith("mutants --in-diff") for c in ran)
    assert cleaned == []


def mutation_fakes(p, monkeypatch, outputs):
    """proc.run returns ``outputs`` in turn for mutation commands; git is a stub at head h<n>."""
    runs = []

    def run(cmd, cwd, **kw):
        runs.append(cmd)
        return proc_mod.Result(0, outputs[min(len(runs), len(outputs)) - 1], "")

    monkeypatch.setattr(proc_mod, "run", run)
    p.git = type("G", (), {"diff": lambda self, *a, **kw: "d", "restore": lambda self, *a: None,
                           "is_clean": lambda self, *a: True,
                           "head": lambda self, *a: f"h{len(runs)}"})()
    return runs


def test_mutation_pass_sends_missed_mutants_back_before_review(pl, tmp_path, monkeypatch):
    from agentpipe.claude import ClaudeResult
    p, _ = pl
    p.t.mutation = "mutants --in-diff {diff}"
    p.wt = lambda t: tmp_path / "wt" / t.slug
    (tmp_path / "wt").mkdir()
    runs = mutation_fakes(p, monkeypatch, ["MISSED   a.rs:3:1: replace < with <=\n1 missed", "0 missed"])
    asked = []
    monkeypatch.setattr(p, "agent", lambda t, prompt, slot, tag, **kw: (asked.append((tag, prompt)), ClaudeResult(
        True, "", "", {"status": "done", "summary": "", "tests": "now pins <"}, False, subtype="success"))[1])
    out = p.mutation_pass(p.tasks["c/1.1"], "impl-0", {"status": "done", "summary": "s", "tests": "t"})
    assert len(runs) == 2 and len(asked) == 1 and asked[0][0] == "mutants"
    assert "replace < with <=" in asked[0][1]
    assert out == {"status": "done", "summary": "s", "tests": "now pins <"}  # empty summary kept
    assert p.state.task("c/1.1")["mutation"] == {"head": "h2", "output": "0 missed"}


def test_mutation_pass_gives_up_after_the_fix_limit(pl, tmp_path, monkeypatch):
    from agentpipe import pipeline as pipeline_mod
    from agentpipe.claude import ClaudeResult
    p, _ = pl
    p.t.mutation = "mutants --in-diff {diff}"
    p.wt = lambda t: tmp_path / "wt" / t.slug
    (tmp_path / "wt").mkdir()
    runs = mutation_fakes(p, monkeypatch, ["MISSED   a.rs:3:1: x"])
    asked = []
    monkeypatch.setattr(p, "agent", lambda *a, **kw: (asked.append(1), ClaudeResult(
        True, "", "", {"status": "done", "summary": "s", "tests": "t"}, False, subtype="success"))[1])
    p.mutation_pass(p.tasks["c/1.1"], "impl-0", {"status": "done", "summary": "s", "tests": "t"})
    assert len(asked) == pipeline_mod.MUTATION_FIXES and len(runs) == pipeline_mod.MUTATION_FIXES + 1


def test_reviewer_reuses_the_mutation_result_for_the_same_commit(pl, tmp_path, monkeypatch):
    from agentpipe.claude import ClaudeResult
    p, _ = pl
    p.t.mutation = "mutants --in-diff {diff}"
    rv = tmp_path / "rv"
    rv.mkdir()
    started = []
    monkeypatch.setattr(proc_mod, "Background", lambda *a, **kw: started.append(a))
    p.git = type("G", (), {"diff": lambda self, *a, **kw: "", "restore": lambda self, *a: None})()
    prompts = []
    verdict = {"verdict": "approve", "summary": "s", "findings": [], "probes": [], "tickets": []}
    p.claude = type("C", (), {"run": lambda self, prompt, *a, **kw: (prompts.append(prompt), ClaudeResult(
        True, "", "", verdict, False, subtype="success"))[1]})()
    p.state.task("c/1.1")["mutation"] = {"head": "abc1234567", "output": "STORED 0 missed"}
    pr = {"number": 7, "headRefOid": "abc1234567", "body": "## Summary\nx"}
    p.reviewer_verdict(p.tasks["c/1.1"], pr, rv, rv, "review-0", [])
    assert started == [] and "STORED 0 missed" in prompts[0]
    p.state.task("c/1.1")["mutation"]["head"] = "older"  # a newer push: run it again
    monkeypatch.setattr(proc_mod, "Background", lambda *a, **kw: (started.append(a), type(
        "B", (), {"result": lambda self: proc_mod.Result(0, "fresh", "")})())[1])
    p.reviewer_verdict(p.tasks["c/1.1"], pr, rv, rv, "review-0", [])
    assert len(started) == 1 and "fresh" in prompts[1]


def test_reviewer_sees_human_input_on_the_issue(pl, tmp_path):
    from agentpipe.claude import ClaudeResult
    p, gh = pl
    p.t.mutation = ""
    rv = tmp_path / "rv"
    rv.mkdir()
    gh.comments_by[3] = [{"user": "max", "created_at": "2026-11-07", "body": "Use option B."}]
    p.git = type("G", (), {"diff": lambda self, *a, **kw: ""})()
    prompts = []
    verdict = {"verdict": "approve", "summary": "s", "findings": [], "probes": [], "tickets": []}
    p.claude = type("C", (), {"run": lambda self, prompt, *a, **kw: (prompts.append(prompt), ClaudeResult(
        True, "", "", verdict, False, subtype="success"))[1]})()
    pr = {"number": 7, "headRefOid": "abc1234567", "body": "## Summary\nx"}
    p.reviewer_verdict(p.tasks["c/1.1"], pr, rv, rv, "review-0", [], 3)
    assert "Input from humans" in prompts[0] and "Use option B." in prompts[0]


def test_clean_workspace_crates_prunes_checkout_scratch(pl, tmp_path, monkeypatch):
    p, _ = pl
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "Cargo.toml").write_text('[workspace]\nmembers = []\nresolver = "2"\n')
    tmp = p.t.work_dir / "target" / "impl-0" / "tmp"
    (tmp / "checkout-abc" / "target").mkdir(parents=True)
    (tmp / "other").mkdir()
    monkeypatch.setattr(proc_mod, "run", lambda *a, **kw: proc_mod.Result(0, '{"packages": []}', ""))
    p.clean_workspace_crates(ws, "impl-0")
    assert (tmp / "checkout-abc").exists()  # off by default
    p.t.clean_workspace_crates = True
    p.clean_workspace_crates(ws, "impl-0")
    assert not (tmp / "checkout-abc").exists()
    assert (tmp / "other").exists()
    p.clean_workspace_crates(ws, "impl-1")  # slot without a target dir: no error


def test_approved_ticket_waits_for_the_tasks_it_names(pl):
    p, gh = pl
    n = gh.add(TICKET_MARKER + "\nNeeds c/1.2 first.", S.PENDING_IMPLEMENTATION, extra=("kind: defect",))
    p.state.data["tickets"] = [n]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    key = f"ticket/{n}"
    p.tick()
    assert key not in {k for _, k, _ in p.started}
    assert p.state.task(key)["waits_for"] == ["c/1.2"]
    p.tasks["c/1.2"] = dataclasses.replace(p.tasks["c/1.2"], done=True)
    p.started.clear(); p.jobs.clear()
    p.tick()
    assert ("implement", key, "impl-0") in p.started or ("implement", key, "impl-1") in p.started


def test_usage_near_a_limit_pauses_new_work_until_the_window_resets(pl):
    p, gh = pl
    soon = time.time() + 3600
    p.note_rate({"five_hour": {"utilization": 0.5, "resetsAt": soon, "status": "allowed"}})
    assert p.state.data["paused_until"] == 0.0
    p.note_rate({"five_hour": {"utilization": 0.86, "resetsAt": soon, "status": "allowed"}})
    assert p.state.data["paused_until"] == soon + 60
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    p.tick()
    assert p.started == []  # paused: nothing new starts
    # A hard limit pauses until the known reset, not just the fixed backoff.
    later = time.time() + 4 * 3600
    p.state.data["rate"] = {"five_hour": {"utilization": 1.0, "resetsAt": later, "status": "rejected"}}
    p.pause("You've hit your 5-hour limit")
    assert p.state.data["paused_until"] == later + 60


def test_config_edits_apply_without_restart(pl, tmp_path, monkeypatch):
    p, _ = pl
    from agentpipe import config as config_mod
    path = tmp_path / "pipeline.toml"
    path.write_text("v1", encoding="utf-8")
    p.watch_config(path, {"max_reviewers": 4})
    assert p.cfg.max_reviewers == 4

    def edited(_path):
        if path.read_text(encoding="utf-8") == "broken":
            raise ValueError("bad toml")
        new_t = dataclasses.replace(p.t, test="cargo test", github_repo="o/other")
        return dataclasses.replace(p.cfg, targets={"t": new_t}, max_implementers=5,
                                   max_reviewers=1, claude_bin="other-claude")
    monkeypatch.setattr(config_mod, "load", edited)

    path.write_text("v2", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    p.reload_config()
    assert p.cfg.max_implementers == 5
    assert p.cfg.max_reviewers == 4  # the command-line override still wins
    assert p.t.test == "cargo test"
    assert p.cfg.claude_bin == "claude" and p.t.github_repo == "o/r"  # restart-only settings kept

    path.write_text("broken", encoding="utf-8")
    os.utime(path, (time.time() + 10, time.time() + 10))
    p.reload_config()
    assert p.cfg.max_implementers == 5  # a broken file keeps the current settings


# ------------------------------------------------------------------ merge conflicts


def touching(p, **paths):
    for k, ps in paths.items():
        p.plan.tasks["c/" + k.replace("_", ".")].touches = ps


def test_a_fresh_task_waits_while_another_in_flight_touches_its_files(pl):
    p, gh = pl
    p.t.overlap_ignore = ["**/lib.rs"]
    touching(p, **{"1_1": ["src/run.rs"], "1_2": ["src/run.rs", "src/lib.rs"], "1_3": ["src/lib.rs"],
                   "1_4": ["src/map.rs"]})
    task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    for k in ("c/1.2", "c/1.3", "c/1.4"):
        task_issue(gh, k, S.PENDING_IMPLEMENTATION)
    gh.prs = [{"number": 20, "headRefOid": "s", "body": TASK_MARKER.format(key="c/1.1"), "files": []}]
    gh.comments_by[20] = [{"body": "<!-- pipeline-review sha=s verdict=request_changes round=1 -->"}]
    p.tick()
    # 1.2 shares run.rs with 1.1's open PR; lib.rs is ignored, so 1.3 and 1.4 go.
    assert [s[1] for s in p.started if s[0] == "implement"] == ["c/1.3", "c/1.4"]
    assert "c/1.2" in p.held and "PR #20" in p.held["c/1.2"]


def test_a_pr_holds_back_tasks_by_the_files_it_actually_changed(pl):
    p, gh = pl
    touching(p, **{"1_2": ["src/run.rs"]})
    task_issue(gh, "c/1.1", S.APPROVED)
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    gh.prs = [{"number": 20, "headRefOid": "s", "body": TASK_MARKER.format(key="c/1.1"),
               "files": [{"path": "src/run.rs"}]}]
    p.tick()
    assert not [s for s in p.started if s[0] == "implement"]
    gh.issues[1]["labels"] = ["pipeline", S.NEEDS_DECISION.label]  # parked with a human: no longer holds
    p.tick()
    assert ("implement", "c/1.2", "impl-0") in p.started


def test_work_under_way_resumes_even_if_it_overlaps(pl):
    p, gh = pl
    touching(p, **{"1_1": ["src/run.rs"], "1_2": ["src/run.rs"]})
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    task_issue(gh, "c/1.2", S.IN_PROGRESS)
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.1", "c/1.2"]


def test_tickets_are_held_back_by_the_files_they_name_under_the_project_dir(pl):
    p, gh = pl
    p.t.project_dir = "game"
    touching(p, **{"1_1": ["game/src/run.rs"]})
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    t = gh.add(TICKET_MARKER + "\n`src/run.rs:12` panics", S.PENDING_IMPLEMENTATION, title="Fix run")
    p.state.data["tickets"] = [t]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.1"]
    assert "c/1.1" in next(iter(p.held.values()))


def merge_repo(tmp_path):
    return make_repo(tmp_path), commit_on, git


def pipeline_on(p, repo):
    from agentpipe.git import Git
    p.git = Git(repo)
    p.base_ref = lambda: "base"
    p.t.project_dir = "."
    return p


def test_merge_base_resolves_additive_conflicts_and_formats(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "base", "lib.rs", "mod a;\nmod c;\nmod z;\n")
    commit_on(repo, "feature", "lib.rs", "mod a;\nmod b;\nmod z;\n")
    pipeline_on(p, repo).t.format = "echo formatted> fmt.txt"
    assert p.merge_base(repo, "s", gate=False) == "auto"
    assert (repo / "lib.rs").read_text() == "mod a;\nmod b;\nmod c;\nmod z;\n"
    assert not p.git.merging(repo) and git(repo, "status", "--porcelain").strip() == "?? fmt.txt"
    assert p.merge_base(repo, "s", gate=False) == "current"


def test_merge_base_leaves_a_real_conflict_for_an_agent(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "base", "run.rs", "x = 1\n")
    commit_on(repo, "feature", "run.rs", "x = 2\n")
    pipeline_on(p, repo)
    assert p.merge_base(repo, "s", gate=False) == "conflict"
    assert p.git.merging(repo) and p.git.conflicted(repo) == ["run.rs"]


def test_a_mechanical_resolution_that_fails_the_gate_goes_to_an_agent(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "base", "lib.rs", "mod a;\nmod c;\nmod z;\n")
    commit_on(repo, "feature", "lib.rs", "mod a;\nmod b;\nmod z;\n")
    before = pipeline_on(p, repo).git.head(repo)
    p.t.test = "exit 1"
    assert p.merge_base(repo, "s", gate=True) == "conflict"
    assert p.git.merging(repo) and p.git.conflicted(repo) == ["lib.rs"]
    assert git(repo, "rev-parse", "HEAD").strip() == before


def test_a_clean_merge_after_approval_is_approved_without_an_agent(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "feature", "run.rs", "x = 1\n")
    pipeline_on(p, repo)
    approved = p.git.head(repo)
    commit_on(repo, "base", "lib.rs", "mod a;\nmod c;\nmod z;\n")
    git(repo, "checkout", "-q", "feature")
    assert p.git.merge(repo, "base")
    history = [{"sha": approved, "verdict": "approve"}, {"sha": approved, "verdict": "conflict"}]
    p.claude = None  # any agent call would fail
    t = p.tasks["c/1.1"]
    v = p.resolution_verdict(t, {"number": 5, "headRefOid": p.git.head(repo)}, repo, repo, "s", history)
    assert v["verdict"] == "approve"
    # New work since the approval needs the full review.
    commit_on(repo, "feature", "run.rs", "x = 9\n")
    assert p.resolution_verdict(t, {"number": 5, "headRefOid": p.git.head(repo)}, repo, repo, "s", history) is None


def archive_repo(p, tmp_path, tasks_md):
    """A remote with `main` holding change `c`, and ``p`` pointed at a clone of it."""
    import sys
    from agentpipe.git import Git
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "root"
    git(tmp_path, "clone", "-q", str(origin), str(root))
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("core.autocrlf", "false")):
        git(root, "config", k, v)
    (root / "openspec" / "changes" / "c").mkdir(parents=True)
    (root / "openspec" / "changes" / "c" / "tasks.md").write_text(tasks_md)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    git(root, "push", "-q", "origin", "HEAD:main")
    p.git = Git(root)
    p.base_wt = tmp_path / "base-wt"
    p.base_ref = lambda: "origin/main"
    p.t.base = "main"
    mover = ("import os,shutil;os.makedirs('openspec/changes/archive',exist_ok=True);"
             "shutil.move('openspec/changes/{change}','openspec/changes/archive/2026-01-01-{change}')")
    p.t.archive = f'"{sys.executable}" -c "{mover}"'
    return origin


def remote_files(origin):
    return git(origin, "ls-tree", "-r", "--name-only", "main").split()


def test_a_finished_change_is_archived_on_the_base_branch(pl, tmp_path):
    p, gh = pl
    origin = archive_repo(p, tmp_path, "- [x] 1.1 a\n- [x] 1.2 b\n")
    p.archive_if_finished("c")
    assert remote_files(origin) == ["openspec/changes/archive/2026-01-01-c/tasks.md"]
    assert gh.issues == {}


def test_a_change_with_an_open_task_is_not_archived(pl, tmp_path):
    p, gh = pl
    origin = archive_repo(p, tmp_path, "- [x] 1.1 a\n- [ ] 1.2 b\n")
    p.archive_if_finished("c")
    assert remote_files(origin) == ["openspec/changes/c/tasks.md"]


def test_a_failed_archive_files_a_ticket_and_pushes_nothing(pl, tmp_path):
    p, gh = pl
    origin = archive_repo(p, tmp_path, "- [x] 1.1 a\n")
    p.t.archive = "echo delta does not apply&& exit 1"
    p.archive_if_finished("c")
    assert remote_files(origin) == ["openspec/changes/c/tasks.md"]
    (ticket,) = gh.issues.values()
    assert ticket["title"] == "Archive the finished change c" and "delta does not apply" in ticket["body"]


def test_archiving_off_leaves_a_finished_change(pl, tmp_path):
    p, gh = pl
    origin = archive_repo(p, tmp_path, "- [x] 1.1 a\n")
    p.t.archive = ""
    p.archive_if_finished("c")
    assert remote_files(origin) == ["openspec/changes/c/tasks.md"]


def test_a_listed_change_that_was_archived_is_no_longer_loaded(tmp_path):
    (tmp_path / "openspec" / "changes" / "live").mkdir(parents=True)
    (tmp_path / "openspec" / "changes" / "live" / "tasks.md").write_text("- [ ] 1.1 a\n")
    t = Target("t", tmp_path, "o/r", "main", ["live", "gone"], ".", "true", "", [], tmp_path / "work")
    assert t.changes(tmp_path) == ["live"]


def test_review_worktree_gets_the_base_merged_in_locally(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "base", "run.rs", "x = 1\n")
    commit_on(repo, "feature", "lib.rs", "mod a;\nmod b;\nmod z;\n")
    pipeline_on(p, repo)
    head = git(repo, "rev-parse", "HEAD").strip()
    p.merge_base_for_review(repo)
    assert (repo / "run.rs").read_text() == "x = 1\n"
    assert p.git.contains(repo, "base") and p.git.contains(repo, head)
    assert "lib.rs" in p.git.diff(repo, "base") and "run.rs" not in p.git.diff(repo, "base")


def test_review_worktree_keeps_the_branch_when_the_base_conflicts(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "base", "run.rs", "x = 1\n")
    commit_on(repo, "feature", "run.rs", "x = 2\n")
    pipeline_on(p, repo)
    head = git(repo, "rev-parse", "HEAD").strip()
    p.merge_base_for_review(repo)
    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert not p.git.merging(repo) and git(repo, "status", "--porcelain").strip() == ""
