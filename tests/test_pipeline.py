"""Scheduler and ticket handling against an in-memory GitHub; jobs are recorded, not run."""

import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

from agentpipe import proc as proc_mod
from agentpipe import review
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

    def list_issues(self, state="all"):
        return [dict(i, labels=list(i["labels"])) for i in self.issues.values()
                if state == "all" or i["state"].lower() == state]

    def issue(self, number):
        i = self.issues[number]
        return {"number": number, "state": i["state"], "body": i["body"], "labels": list(i["labels"])}

    def list_prs(self):
        return self.prs

    def merged_pr(self, branch):
        return next((p for p in getattr(self, "merged", []) if p["headRefName"] == branch), None)

    def create_issue(self, title, body, status, extra_labels=()):
        return self.add(body, status, title, extra=extra_labels)

    def set_status(self, number, new, old):
        labels = [l for l in self.issues[number]["labels"] if not l.startswith("status: ")]
        self.issues[number]["labels"] = labels + [new.label]

    def comment(self, number, body):
        self.comments_by.setdefault(number, []).append({"body": body, "user": "me", "created_at": "now"})

    def comments(self, number):
        return self.comments_by.get(number, [])

    def label_added_at(self, number, label):
        return self.label_times.get((number, label), "") if hasattr(self, "label_times") else ""

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


def test_earlier_milestone_starts_before_later_milestones_resumed_work(pl):
    from agentpipe.config import Milestone
    p, gh = pl
    p.t.milestones = [Milestone("m1", "", ["d"]), Milestone("m2", "", ["c"])]
    d = p.base_wt / "openspec" / "changes" / "d"
    d.mkdir(parents=True)
    (d / "tasks.md").write_text("- [ ] 1.1 x\n- [ ] 1.2 y\n")
    p.t.changes_spec = ["c", "d"]
    p.tasks.update({t.key: t for t in parse_tasks("d", "- [ ] 1.1 x\n- [ ] 1.2 y\n")})
    p.plan = Plan("fp", {k: TaskDeps() for k in p.tasks})
    task_issue(gh, "c/1.1", S.IN_PROGRESS)  # later milestone, under way
    task_issue(gh, "d/1.2", S.PENDING_IMPLEMENTATION)  # earlier milestone, fresh
    task_issue(gh, "d/1.1", S.IN_PROGRESS)  # earlier milestone, under way: resumes first
    p.tick()
    assert p.started == [("implement", "d/1.1", "impl-0"), ("implement", "d/1.2", "impl-1")]


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


def test_a_reviewed_head_set_back_to_pending_review_is_reviewed_again(pl):
    p, gh = pl
    n = task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    gh.prs = [{"number": 11, "headRefOid": "sha1", "body": TASK_MARKER.format(key="c/1.1")}]
    gh.comments_by[11] = [{"body": "<!-- pipeline-review sha=sha1 verdict=tests_failed round=3 -->",
                           "created_at": "2026-11-08T00:27:10Z"}]
    gh.label_times = {(n, S.PENDING_REVIEW.label): "2026-11-08T00:20:00Z"}  # set before the review
    p.tick()
    # The label never followed that review: its verdict is applied, not reviewed again.
    assert p.started == [] and S.CHANGES_REQUESTED.label in gh.issues[n]["labels"]
    gh.set_status(n, S.PENDING_REVIEW, None)
    gh.label_times = {(n, S.PENDING_REVIEW.label): "2026-11-08T09:00:00Z"}  # set again afterwards
    p.tick()
    assert p.started == [("review", "c/1.1", "review-0")]


def test_a_rounds_reset_gives_a_pr_fresh_rounds_but_keeps_its_feedback():
    rejected = "<!-- pipeline-review sha={} verdict=tests_failed round={} -->"
    comments = [{"body": rejected.format("a", 1)}, {"body": rejected.format("b", 2)},
                {"body": "Out of rounds. " + review.ROUNDS_RESET_MARKER}]
    history = review.review_history(comments)
    assert review.rounds_used(history) == 0 and history[-1]["sha"] == "b"  # revise still reads it
    history = review.review_history(comments + [{"body": rejected.format("c", 1)}])
    assert review.rounds_used(history) == 1


def test_the_oldest_approval_merges_first_whatever_its_milestone(pl):
    p, gh = pl
    task_issue(gh, "c/1.3", S.APPROVED)
    task_issue(gh, "c/1.4", S.APPROVED)
    gh.prs = [{"number": 10 + i, "headRefOid": f"sha{i}", "body": TASK_MARKER.format(key=f"c/1.{i}")}
              for i in (3, 4)]
    p.state.task("c/1.3")["approved_at"] = 200.0
    p.state.task("c/1.4")["approved_at"] = 100.0
    p.tick()
    assert [s for s in p.started if s[0] == "merge"] == [("merge", "c/1.4", "merge")]


def test_an_approval_gated_on_the_current_base_merges_before_older_ones(pl, monkeypatch):
    p, gh = pl
    task_issue(gh, "c/1.3", S.APPROVED)
    task_issue(gh, "c/1.4", S.APPROVED)
    gh.prs = [{"number": 10 + i, "headRefOid": f"sha{i}", "body": TASK_MARKER.format(key=f"c/1.{i}")}
              for i in (3, 4)]
    p.state.task("c/1.3").update(approved_at=100.0, gated={"head": "sha3", "base": "old"})
    p.state.task("c/1.4").update(approved_at=200.0, gated={"head": "sha4", "base": "now"})
    monkeypatch.setattr(p, "current_base", lambda: "now")
    p.tick()
    assert [s for s in p.started if s[0] == "merge"] == [("merge", "c/1.4", "merge")]


def merge_job(p, gh, monkeypatch, *, gated, base_now):
    """Run job_merge on an approved c/1.1 with stubbed git; returns the gates it ran."""
    n = task_issue(gh, "c/1.1", S.APPROVED)
    pr = {"number": 7, "headRefOid": "head1"}
    p.state.task("c/1.1")["gated"] = gated
    calls = {"gates": 0, "merged": []}

    class G:
        def fetch(self): pass
        def git(self, *a, **kw): pass
        def head(self, wt): return "head1"
        def contains(self, wt, ref): return False
        def rev(self, wt, ref): return base_now
        def push(self, wt, branch): pass

    p.git = G()
    p.ensure_worktree = lambda t: Path(".")
    monkeypatch.setattr(p, "merge_base", lambda wt, slot, gate: "clean")
    monkeypatch.setattr(p, "run_gate", lambda *a, **kw: (calls.__setitem__("gates", calls["gates"] + 1),
                                                         proc_mod.Result(0, "", ""))[1])
    gh.merge_pr = lambda number, sha: calls["merged"].append(number)
    gh.close_issue = lambda number, reason="completed": None
    # What follows a merge (ticking tasks.md, releasing waiters) isn't under test here.
    monkeypatch.setattr(p, "after_merge", lambda *a, **kw: None, raising=False)
    try:
        p.job_merge(p.tasks["c/1.1"], n, pr, "merge")
    except (AttributeError, TypeError):
        pass  # post-merge bookkeeping needs a real repo; the merge itself already happened
    return calls


def test_merge_skips_the_gate_when_the_review_gated_the_same_head_and_base(pl, monkeypatch):
    p, gh = pl
    calls = merge_job(p, gh, monkeypatch, gated={"head": "head1", "base": "base1"}, base_now="base1")
    assert calls == {"gates": 0, "merged": [7]}


def test_merge_runs_the_gate_when_the_base_moved_since_the_review(pl, monkeypatch):
    p, gh = pl
    calls = merge_job(p, gh, monkeypatch, gated={"head": "head1", "base": "base1"}, base_now="base2")
    assert calls == {"gates": 1, "merged": [7]}


def test_merge_runs_the_gate_when_the_review_gated_only_the_branch(pl, monkeypatch):
    p, gh = pl
    calls = merge_job(p, gh, monkeypatch, gated=None, base_now="base1")
    assert calls == {"gates": 1, "merged": [7]}


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


def test_waiting_count_skips_closed_issues_and_counts_tickets_before_a_sync(pl):
    p, gh = pl
    closed = task_issue(gh, "c/1.1", S.NEEDS_DECISION)
    gh.issues[closed]["state"] = "CLOSED"  # closed by hand with its old label left on
    fresh = gh.add(TICKET_MARKER + "\nround shares", S.PROPOSED, title="Rounding")  # not loaded yet
    p.tick()
    assert p.state.data["awaiting_input"] == [fresh]


def test_waiting_count_refreshes_while_the_main_loop_is_held_up(pl):
    import threading
    p, gh = pl
    p.env_ok = False  # preflight gate still running: schedule() returns early
    d = task_issue(gh, "c/1.1", S.NEEDS_DECISION)
    p.cfg.poll_seconds = 0.01
    done = threading.Event()
    t = threading.Thread(target=p.refresh_awaiting_forever, args=(done,))
    t.start()
    deadline = time.time() + 5
    while p.state.data.get("awaiting_input") != [d] and time.time() < deadline:
        time.sleep(0.01)
    done.set()
    t.join(5)
    assert p.state.data["awaiting_input"] == [d]
    assert not t.is_alive()


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


def _code_pipeline(p, monkeypatch, revs, imports=""):
    """Point p's self-restart at a fake checkout whose HEAD steps through ``revs``."""
    from agentpipe import service
    p.cfg.poll_seconds = 0
    p.CODE_CHECK_SECONDS = 0
    p.config_path = p.t.work_dir / "pipeline.toml"
    p.code_rev = revs[0]
    it = iter(revs)
    p.read_code_rev = lambda: next(it, revs[-1])
    p.code_imports = lambda: imports
    started = []
    p.env_ok = True  # preflight passed here, so the successor may skip its own
    monkeypatch.setattr(service, "start",
                        lambda name, wd, cfg, handed_over=False: (started.append(name if handed_over else "PREFLIGHT"), 777)[1])
    return started


def test_new_agentpipe_commit_hands_over_at_once_and_finishes_running_jobs(pl, monkeypatch):
    import json
    from agentpipe import handoff
    p, gh = pl
    from agentpipe import service
    started = _code_pipeline(p, monkeypatch, ["a" * 40, "b" * 40])
    monkeypatch.setattr(service, "is_alive", lambda pid: pid in (777, os.getpid()))
    lines, ticks, files = [], [], []
    p._log = lines.append
    job = type("J", (), {"kind": "implement", "slot": "impl-0", "issue": 5})()
    mine = handoff.handoff_dir(p.t.work_dir) / f"{os.getpid()}.json"

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            p.jobs["c/1.1"] = job
            p.jobs["c/1.2"] = job
        elif len(ticks) == 2:
            assert started == ["t"] and p.stopping  # handed over before this tick: nothing new
            files.append(json.loads(mine.read_text(encoding="utf-8"))["jobs"])
            p.jobs.pop("c/1.2")
            p.state.task("c/1.1")["runs"] = 1
        elif len(ticks) == 3:
            files.append(json.loads(mine.read_text(encoding="utf-8"))["jobs"])
            p.jobs.clear()
        assert len(ticks) < 10

    p.tick = tick
    p.run_forever()
    assert set(files[0]) == {"c/1.1", "c/1.2"} and files[0]["c/1.1"]["issue"] == 5
    assert set(files[1]) == {"c/1.1"}  # rewritten as jobs end
    assert any("handed over to a new dispatcher; finishing 2 running job(s)" in l for l in lines)
    assert p.pid_file.read_text(encoding="utf-8") == "777"  # the new dispatcher's
    assert not mine.exists()  # removed once its last job ended
    assert p.state.own_only == {"c/1.1", "c/1.2"}


def test_handoff_waits_for_a_running_merge(pl, monkeypatch):
    p, gh = pl
    started = _code_pipeline(p, monkeypatch, ["a" * 40, "b" * 40])
    ticks = []
    merge = type("J", (), {"kind": "merge", "slot": "merge"})()
    p.jobs["c/1.1"] = merge

    def tick():
        ticks.append(1)
        if len(ticks) == 2:
            assert p.restart_for and not started  # pending: no handoff while it merges
            p.jobs.clear()
        elif len(ticks) == 3:
            assert started == ["t"]
        assert len(ticks) < 10

    p.tick = tick
    p.run_forever()
    assert started == ["t"]


def test_old_dispatcher_takes_back_over_when_its_successor_dies(pl, monkeypatch):
    import json
    from agentpipe import handoff, service, watch
    p, gh = pl
    started = _code_pipeline(p, monkeypatch, ["a" * 40, "b" * 40])
    alive = {777: True}
    monkeypatch.setattr(service, "is_alive", lambda pid: pid == os.getpid() or alive.get(pid, False))
    lines, ticks = [], []
    p._log = lines.append
    job = type("J", (), {"kind": "implement", "slot": "impl-0", "issue": 5})()
    mine = handoff.handoff_dir(p.t.work_dir) / f"{os.getpid()}.json"
    holder = type("H", (), {"seen_dispatcher": False})()

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            p.jobs["c/1.1"] = job
            p.state.task("c/1.1")["runs"] = 1
        elif len(ticks) == 2:
            assert p.handing_off and mine.exists()
            # Meanwhile the new dispatcher wrote another task, then died.
            disk = p.state.read()
            disk["tasks"]["c/2.1"] = {"runs": 7}
            p.state.path.write_text(json.dumps(disk), encoding="utf-8")
            alive[777] = False
            assert not watch.linked_watcher_done(p.t.work_dir, holder)  # we still finish jobs
        elif len(ticks) == 3:
            # Taken back with the dead one's writes, then handed over again at once.
            assert p.state.task("c/2.1") == {"runs": 7} and p.state.task("c/1.1") == {"runs": 1}
            assert started == ["t", "t"] and p.handing_off and mine.exists()
            alive[777] = False  # dies again
        elif len(ticks) == 4:
            assert started == ["t", "t"] and not p.handing_off and not p.stopping
            assert p.restart_for is None and p.state.own_only is None
            assert p.pid_file.read_text(encoding="utf-8") == str(os.getpid()) and not mine.exists()
            assert not watch.linked_watcher_done(p.t.work_dir, holder)
            p.stop_file.write_text("", encoding="utf-8")
            p.jobs.clear()
        assert len(ticks) < 10

    def start(name, wd, cfg, handed_over=False):
        started.append(name)
        alive[777] = True
        return 777

    monkeypatch.setattr(service, "start", start)
    p.tick = tick
    p.run_forever()
    assert any("died; taking the target back" in l and "handing over again" in l for l in lines)
    assert any("staying on this code until the next commit" in l for l in lines)
    assert p.bad_rev == "b" * 40


def test_a_github_blip_in_a_job_is_not_counted_as_a_failure(pl):
    from concurrent.futures import Future
    p, gh = pl
    lines = []
    p._log = lines.append
    fut = Future()
    fut.set_exception(proc_mod.CommandError(["gh", "pr", "create"],
                                            proc_mod.Result(1, "", "net/http: TLS handshake timeout")))
    p.jobs["c/1.1"] = type("J", (), {"kind": "implement", "slot": "impl-0", "future": fut})()
    p.reap()
    assert "fail_implement" not in p.state.task("c/1.1")
    assert any("implement c/1.1 crashed: GitHub unreachable" in l for l in lines)


def test_new_dispatcher_leaves_handed_over_work_and_slots_alone(pl, monkeypatch):
    from agentpipe import handoff, service
    p, gh = pl
    monkeypatch.setattr(service, "is_alive", lambda pid: pid == 4242)
    handoff.write(p.t.work_dir, 4242, {"c/1.3": {"kind": "implement", "slot": "impl-0", "issue": 9,
                                                 "batch": ["c/1.3", "c/1.4"]}})
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    task_issue(gh, "c/1.3", S.IN_PROGRESS)  # would be resumed if it weren't handed over
    task_issue(gh, "c/1.4", S.IN_PROGRESS)  # its batch follower
    lines = []
    p._log = lines.append
    p.tick()
    assert p.started == [("implement", "c/1.1", "impl-1")]  # impl-0 is the old dispatcher's
    assert any(l.endswith("carried over implement c/1.3 (#9) on impl-0: old dispatcher 4242 finishes it")
               for l in lines)
    from agentpipe import watch
    held = [l[l.index("carried over"):] for l in lines if "carried over" in l]
    jobs = watch.parse_log([f"2026-11-09 14:29:11 [t] {l}" for l in held]).jobs
    assert [(j.kind, j.key, j.slot) for j in jobs] == [("implement", "c/1.3", "impl-0")]
    # The old dispatcher finishes: its record of the task is taken over from state.json.
    disk = p.state.read()
    disk["tasks"]["c/1.3"] = {"runs": 3}
    p.state.path.write_text(__import__("json").dumps(disk), encoding="utf-8")
    handoff.remove(p.t.work_dir, 4242)
    p.jobs.clear()
    synced = []
    p.sync = lambda on_ready: synced.append(1)  # finished work may unblock others: sync now
    p.tick()
    assert synced
    assert p.state.task("c/1.3") == {"runs": 3}
    assert any(l.endswith("done implement c/1.3") for l in lines)


def test_work_the_old_dispatcher_finishes_mid_tick_frees_its_slot_at_once(pl, monkeypatch):
    from agentpipe import handoff, service
    p, gh = pl
    monkeypatch.setattr(service, "is_alive", lambda pid: pid == 4242)
    handoff.write(p.t.work_dir, 4242, {"c/1.3": {"kind": "implement", "slot": "impl-0", "issue": 9,
                                                 "batch": ["c/1.3"]}})
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    p.env_ok = False

    def preflight_meanwhile():  # takes minutes for real; the old job ends during it
        handoff.write(p.t.work_dir, 4242, {})
        p.env_ok = True

    p.check_environment = preflight_meanwhile
    p.tick()
    assert p.started == [("implement", "c/1.1", "impl-0"), ("implement", "c/1.2", "impl-1")]


def test_dead_old_dispatchers_handoff_is_ignored(pl, monkeypatch):
    from agentpipe import handoff, service
    p, gh = pl
    monkeypatch.setattr(service, "is_alive", lambda pid: False)
    handoff.write(p.t.work_dir, 4242, {"c/1.1": {"kind": "implement", "slot": "impl-0", "batch": ["c/1.1"]}})
    assert handoff.live(p.t.work_dir) == {}
    assert not list(handoff.handoff_dir(p.t.work_dir).glob("*.json"))


def test_state_merges_two_dispatchers_writes(tmp_path):
    from agentpipe.pipeline import State
    path = tmp_path / "state.json"
    s = State(path)
    s.task("old")["runs"] = 1
    s.task("new")["runs"] = 1
    s.save()
    old, new = State(path), State(path)
    old.own_only, new.foreign = {"old"}, {"old"}
    old.task("old")["runs"] = 2
    old.task("new")["runs"] = 99  # not its task: never written
    old.data["paused_until"] = 50.0
    old.save()
    new.task("new")["runs"] = 2
    new.task("old")["runs"] = 77  # not its task while handed over: the file's value wins
    new.save()
    disk = State(path).data
    assert disk["tasks"] == {"old": {"runs": 2}, "new": {"runs": 2}}
    assert disk["paused_until"] == 50.0  # the old one's usage pause is kept
    old.task("old")["runs"] = 3
    old.save()
    new.foreign = set()
    new.refresh({"old"})
    assert new.task("old") == {"runs": 3}


def test_tickets_an_old_dispatcher_files_become_the_new_ones(tmp_path):
    from agentpipe.pipeline import State
    path = tmp_path / "state.json"
    s = State(path)
    s.data["tickets"] = [1]
    s.save()
    old, new = State(path), State(path)
    old.own_only, new.foreign = {"x"}, {"x"}
    new.data["tickets"].append(3)
    new.save()
    old.data["tickets"].append(2)  # filed while finishing its jobs
    old.save()
    assert State(path).data["tickets"] == [1, 2, 3]  # the old one keeps the new one's filings
    new.save()
    assert new.data["tickets"] == [1, 2, 3]  # so the new one triages and works #2
    old.data["tickets"].append(4)
    old.save()
    new.foreign = set()
    new.refresh({"x"})
    assert 4 in new.data["tickets"]


def test_commit_that_fails_to_import_is_not_restarted_onto(pl, monkeypatch):
    p, gh = pl
    started = _code_pipeline(p, monkeypatch, ["a" * 40, "b" * 40], imports="SyntaxError: bad")
    lines = []
    p._log = lines.append
    p.had_watcher = False
    p.check_code()
    p.check_code()
    assert p.restart_for is None and p.stop_reason() is None and not started
    assert sum("doesn't import" in l for l in lines) == 1  # logged once per commit


def test_stop_request_beats_a_pending_handoff(pl, monkeypatch):
    p, gh = pl
    started = _code_pipeline(p, monkeypatch, ["a" * 40, "b" * 40])
    ticks = []

    def tick():
        ticks.append(1)
        p.stop_file.write_text("", encoding="utf-8")
        assert len(ticks) < 5

    p.tick = tick
    p.run_forever()
    assert not started and not p.pid_file.exists()


def test_github_outage_in_a_tick_does_not_crash_the_dispatcher(pl):
    p, gh = pl
    p.cfg.poll_seconds = 0
    lines, ticks = [], []
    p._log = lines.append

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise proc_mod.CommandError(["gh", "pr", "list"], proc_mod.Result(1, "", "net/http: TLS handshake timeout"))
        if len(ticks) == 2:
            p.stop_file.write_text("", encoding="utf-8")

    p.tick = tick
    p.run_forever()
    assert any("GitHub unreachable, trying again next tick" in l for l in lines)
    assert len(ticks) >= 2 and lines[-1].endswith("stopped")


def test_other_command_errors_do_not_crash_the_dispatcher_either(pl):
    p, gh = pl
    p.cfg.poll_seconds = 0
    lines, ticks = [], []
    p._log = lines.append

    def tick():
        ticks.append(1)
        if len(ticks) == 3:
            p.stop_file.write_text("", encoding="utf-8")
        if len(ticks) < 3:
            raise proc_mod.CommandError(["gh", "pr", "merge"], proc_mod.Result(1, "", "HTTP 422: not mergeable"))

    p.tick = tick
    p.run_forever()
    assert sum("tick failed" in l and "HTTP 422" in l for l in lines) == 1  # logged once, not per tick
    assert len(ticks) >= 3 and lines[-1].endswith("stopped")


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


def test_cap_cache_deletes_largest_idle_slots_down_to_three_quarters(pl):
    p, _ = pl
    mb = 2**20
    for root, slot, size in (("target", "impl-0", 40), ("target", "impl-1", 30), ("target", "review-0", 20),
                             ("tmp", "impl-1", 5), ("target", "merge", 10)):
        d = p.t.work_dir / root / slot
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob").write_bytes(b"\0" * size * mb)
    p.cap_cache()
    assert (p.t.work_dir / "target" / "impl-1").exists()  # no cap by default
    p.jobs["k"] = _Job("implement", "impl-0")  # busy: never touched, though largest
    p.t.cache_max_gb = 100 / 1024  # 105 MB in all, cap 100 MB, so down to 75 MB
    p.cap_cache()
    left = {f"{d.parent.name}/{d.name}" for r in ("target", "tmp") for d in (p.t.work_dir / r).iterdir()}
    assert left == {"target/impl-0", "target/review-0", "tmp/impl-1", "target/merge"}
    p.t.cache_max_gb = 1  # under the cap: nothing more goes
    p.cap_cache()
    assert len(list((p.t.work_dir / "target").iterdir())) == 3


def test_dir_bytes_counts_a_hard_linked_file_once(tmp_path):
    from agentpipe.pipeline import dir_bytes
    big = tmp_path / "a" / "engine.exe"
    big.parent.mkdir()
    big.write_bytes(b"\0" * 2**21)
    (tmp_path / "b").mkdir()
    os.link(big, tmp_path / "b" / "engine.exe")
    (tmp_path / "a" / "small").write_bytes(b"\0" * 10)
    assert dir_bytes(tmp_path) == 2**21 + 10
    seen: set[int] = set()  # shared across dirs: the second dir's link is already counted
    assert dir_bytes(tmp_path / "a", seen) + dir_bytes(tmp_path / "b", seen) == 2**21 + 10


def test_cap_cache_drops_idle_slots_checkout_scratch_and_keeps_their_builds(pl):
    p, _ = pl
    p.t.cache_max_gb = 1
    for slot in ("impl-0", "impl-1"):
        (p.t.work_dir / "target" / slot / "tmp" / "checkout-abc").mkdir(parents=True)
        (p.t.work_dir / "target" / slot / "debug").mkdir(parents=True)
    p.jobs["k"] = _Job("implement", "impl-0")
    p.cap_cache()
    assert (p.t.work_dir / "target" / "impl-0" / "tmp" / "checkout-abc").exists()  # busy
    assert not (p.t.work_dir / "target" / "impl-1" / "tmp" / "checkout-abc").exists()
    assert (p.t.work_dir / "target" / "impl-1" / "debug").exists()


def test_cap_cache_logs_once_when_busy_slots_hold_the_cache(pl):
    # 2026-11-09: with every large slot busy, each sync deleted empty dirs and logged it.
    p, _ = pl
    mb = 2**20
    for slot, size in (("impl-0", 60), ("impl-1", 50)):
        d = p.t.work_dir / "target" / slot
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob").write_bytes(b"\0" * size * mb)
    (p.t.work_dir / "target" / "merge").mkdir()  # empty and idle: not worth deleting
    p.jobs["a"] = _Job("implement", "impl-0")
    p.jobs["b"] = _Job("implement", "impl-1")
    p.t.cache_max_gb = 100 / 1024
    logged = []
    p.log = logged.append
    p.cap_cache()
    p.cap_cache()
    assert len(logged) == 1 and "busy slots hold the rest" in logged[0]
    assert (p.t.work_dir / "target" / "merge").exists()
    del p.jobs["b"]  # impl-1 goes idle: it is deleted and logged
    p.cap_cache()
    assert len(logged) == 2 and "deleted target/impl-1" in logged[1] and "merge" not in logged[1]


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


def test_later_work_in_flight_does_not_hold_back_a_milestone_task(pl):
    from agentpipe.milestones import Milestone
    p, gh = pl
    p.t.milestones = [Milestone("m1", "Milestone 1", ["c"])]
    touching(p, **{"1_2": ["src/run.rs"]})
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    t = gh.add(TICKET_MARKER + "\n`src/run.rs:12` panics", S.IN_PROGRESS, title="Fix run")
    p.state.data["tickets"] = [t]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    # The ticket ranks after the milestone's tasks, so its open work doesn't make c/1.2 wait.
    assert "c/1.2" in [s[1] for s in p.started]
    assert "c/1.2" not in p.held


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


def train_setup(p, gh, tmp_path, monkeypatch, branches, gate_ok=True):
    """A repo with `base` and one branch per task in ``branches`` (task key -> (path, text)),
    published as origin/<branch>; returns the merge order and a record of what happened."""
    repo, commit_on, git = merge_repo(tmp_path)
    pipeline_on(p, repo)
    p.git.fetch = lambda: None
    p.base_wt = repo
    p.t.work_dir = tmp_path / "work"
    order, seen = [], {"gates": [], "merged": [], "finished": []}
    for i, (key, (path, text)) in enumerate(branches.items()):
        t = p.tasks[key]
        git(repo, "checkout", "-q", "-b", f"work{i}", "base")
        (repo / path).write_bytes(text.encode())
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", key)
        git(repo, "branch", "-f", f"origin/{t.branch}")
        order.append((t, task_issue(gh, key, S.APPROVED), {"number": 20 + i,
                                                           "headRefOid": git(repo, "rev-parse", "HEAD").strip()}))
    git(repo, "checkout", "-q", "base")
    passes = gate_ok if callable(gate_ok) else (lambda files: gate_ok)

    def run_gate(cwd, *a, **kw):
        files = sorted(f.name for f in Path(cwd).iterdir() if f.suffix == ".rs")
        seen["gates"].append(files)
        return proc_mod.Result(0 if passes(files) else 1, "", "gate output")
    monkeypatch.setattr(p, "run_gate", run_gate)
    gh.merge_pr = lambda n, sha: seen["merged"].append(n)
    monkeypatch.setattr(p, "finish_merge", lambda t, issue, pr: seen["finished"].append(t.key))
    return order, seen


def test_a_merge_batch_gates_once_and_merges_every_pr_in_order(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch,
                              {"c/1.1": ("a.rs", "a\n"), "c/1.2": ("b.rs", "b\n"), "c/1.3": ("c.rs", "c\n")})
    assert p.merge_train(order, "merge") == "merged"
    assert seen["gates"] == [["a.rs", "b.rs", "c.rs", "lib.rs", "run.rs"]]  # one gate, all three together
    assert seen["merged"] == [20, 21, 22] and seen["finished"] == ["c/1.1", "c/1.2", "c/1.3"]


def test_a_pr_that_conflicts_with_the_batch_is_left_for_later(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch,
                              {"c/1.1": ("run.rs", "x = 1\n"), "c/1.2": ("run.rs", "x = 2\n"), "c/1.3": ("c.rs", "c\n")})
    assert p.merge_train(order, "merge") == "merged"
    assert seen["merged"] == [20, 22]  # c/1.2 changed the same line as c/1.1


def test_a_first_pr_that_conflicts_with_the_base_merges_alone_before_any_batch(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch,
                              {"c/1.1": ("a.rs", "a\n"), "c/1.2": ("b.rs", "b\n"), "c/1.3": ("c.rs", "c\n")})
    real_merge = p.git.merge
    monkeypatch.setattr(p.git, "merge", lambda wt, ref: False if ref.endswith(order[0][0].branch) else real_merge(wt, ref))
    p.merge_order = order
    singles = []
    monkeypatch.setattr(p, "merge_one", lambda t, issue, pr, slot: singles.append(t.key))
    p.job_merge(*order[0], "merge")
    assert singles == ["c/1.1"] and seen["gates"] == [] and seen["merged"] == []


FOUR = {"c/1.1": ("a.rs", "a\n"), "c/1.2": ("b.rs", "b\n"), "c/1.3": ("c.rs", "c\n"), "c/1.4": ("d.rs", "d\n")}


def test_a_failed_batch_is_bisected_and_its_passing_front_merges(pl, tmp_path, monkeypatch):
    p, gh = pl
    # c/1.3 breaks the build; everything before it is fine.
    order, seen = train_setup(p, gh, tmp_path, monkeypatch, FOUR, gate_ok=lambda files: "c.rs" not in files)
    assert p.merge_train(order, "merge") == "merged"
    # Whole batch fails (twice: the full-batch gate gets one retry), then the first half passes,
    # then the first three fail: c/1.3 is it.
    assert [len([f for f in g if f in "a.rs b.rs c.rs d.rs"]) for g in seen["gates"]] == [4, 4, 2, 3]
    assert seen["merged"] == [20, 21] and seen["finished"] == ["c/1.1", "c/1.2"]
    assert p.state.task("c/1.3")["batch_culprit"] == order[2][2]["headRefOid"]
    assert (p.logs / "merge-batch-20-21-22.log").read_text() == "gate output"
    assert "batch_culprit" not in p.state.task("c/1.4")  # waits for the next batch


def test_a_pr_that_broke_a_batch_is_left_out_of_the_next_and_merges_alone(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch, FOUR)
    p.state.task("c/1.3")["batch_culprit"] = order[2][2]["headRefOid"]
    p.merge_order = order[2:]
    singles = []
    monkeypatch.setattr(p, "merge_one", lambda t, issue, pr, slot: singles.append(t.key))
    p.job_merge(*order[2], "merge")
    assert singles == ["c/1.3"] and seen["gates"] == []  # no batch around the culprit
    assert p.merge_train(order[2:], "merge") == "too few"  # c/1.4 alone is no batch
    p.state.task("c/1.3")["batch_culprit"] = "an older head"  # revised since: batched again
    assert p.merge_train(order, "merge") == "merged" and seen["merged"] == [20, 21, 22, 23]


def test_a_batch_whose_first_pr_fails_merges_nothing(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch,
                              {"c/1.1": ("a.rs", "a\n"), "c/1.2": ("b.rs", "b\n")}, gate_ok=False)
    p.merge_order = order
    singles = []
    monkeypatch.setattr(p, "merge_one", lambda t, issue, pr, slot: singles.append(t.key))
    p.job_merge(*order[0], "merge")
    # The full batch, its one retry, then the first PR alone.
    assert len(seen["gates"]) == 3 and seen["merged"] == [] and singles == ["c/1.1"]
    assert p.state.task("c/1.1")["batch_culprit"] == order[0][2]["headRefOid"]


def test_one_failing_finish_doesnt_skip_the_rest_of_a_merged_batch(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch, {"c/1.1": ("a.rs", "a\n"), "c/1.2": ("b.rs", "b\n"),
                                                             "c/1.3": ("c.rs", "c\n")})
    in_flight = []

    def finish(t, issue, pr):
        in_flight.append(set(p.merging))
        if t.key == "c/1.2":
            raise RuntimeError("index.lock exists")
        seen["finished"].append(t.key)
    monkeypatch.setattr(p, "finish_merge", finish)
    with pytest.raises(RuntimeError, match="index.lock"):
        p.merge_train(order, "merge")
    assert seen["finished"] == ["c/1.1", "c/1.3"]
    # Merged on GitHub but not yet relabelled: the sync must not call them stuck meanwhile.
    assert in_flight[0] == {"c/1.1", "c/1.2", "c/1.3"} and p.merging == set()


def test_review_worktree_gets_the_base_merged_in_locally(pl, tmp_path):
    p, _ = pl
    repo, commit_on, git = merge_repo(tmp_path)
    commit_on(repo, "base", "run.rs", "x = 1\n")
    commit_on(repo, "feature", "lib.rs", "mod a;\nmod b;\nmod z;\n")
    pipeline_on(p, repo)
    head = git(repo, "rev-parse", "HEAD").strip()
    assert p.merge_base_for_review(repo) == git(repo, "rev-parse", "base").strip()
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
    assert p.merge_base_for_review(repo) is None  # the gate will cover only the branch
    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert not p.git.merging(repo) and git(repo, "status", "--porcelain").strip() == ""


def test_a_target_can_run_fewer_agents_than_run_says(pl):
    p, gh = pl
    p.t.max_implementers, p.t.max_reviewers = 1, 1
    for k in ("c/1.1", "c/1.2", "c/1.3"):
        task_issue(gh, k, S.PENDING_IMPLEMENTATION)
    p.tick()
    assert len([s for s in p.started if s[0] == "implement"]) == 1
    p.overrides = {"max_implementers": 2}  # a command-line count still wins
    assert p.implementers() == p.cfg.max_implementers and p.reviewers() == 1


def test_an_approved_pr_that_conflicts_is_updated_before_its_merge_turn_and_jumps_the_queue(pl):
    p, gh = pl
    p.t.merge_batch = 1
    task_issue(gh, "c/1.1", S.APPROVED)  # next to merge: left to the merge job
    n = task_issue(gh, "c/1.2", S.APPROVED)  # waits behind it, and already conflicts
    task_issue(gh, "c/1.3", S.IN_PROGRESS)  # would otherwise take the first implementer slot
    gh.prs = [{"number": 10 + i, "headRefOid": f"sha{i}", "body": TASK_MARKER.format(key=f"c/1.{i}")}
              for i in (1, 2)]
    p.state.task("c/1.1")["approved_at"] = 100.0
    p.state.task("c/1.2")["approved_at"] = 200.0
    p.current_base = lambda: "base1"
    checked = []
    p.git.merge_conflicts = lambda base, head: checked.append(head) or True
    p.tick()
    assert checked == ["sha2"]  # the next merge isn't checked; conflicts are its merge job's
    assert S.CHANGES_REQUESTED.label in gh.issues[n]["labels"]
    assert "verdict=conflict" in gh.comments_by[12][-1]["body"]
    assert p.started[0] == ("revise", "c/1.2", "impl-0")
    assert [s for s in p.started if s[0] == "merge"] == [("merge", "c/1.1", "merge")]
    assert "conflict_update" in p.state.task("c/1.2")


# ------------------------------------------------------------------ engine and front tracks
def test_front_tasks_take_only_front_slots_and_engine_tasks_only_the_others(pl):
    p, gh = pl
    p.t.front_paths, p.t.front_implementers = ["game/*", "crates/bridge/*"], 1
    p.cfg.max_implementers = 3
    touching(p, **{"1_1": ["game/ui.gd"], "1_2": ["game/menu.gd"], "1_3": ["crates/sim/src/a.rs"],
                   "1_4": ["crates/sim/src/b.rs"]})
    for k in ("c/1.1", "c/1.2", "c/1.3", "c/1.4"):
        task_issue(gh, k, S.IN_PROGRESS)
    p.tick()
    assert sorted(p.started) == [("implement", "c/1.1", "impl-2"), ("implement", "c/1.3", "impl-0"),
                                 ("implement", "c/1.4", "impl-1")]  # c/1.2 waits for the front slot


def test_without_a_split_every_task_shares_the_slots(pl):
    p, gh = pl
    touching(p, **{"1_1": ["game/ui.gd"], "1_2": ["game/menu.gd"]})
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    task_issue(gh, "c/1.2", S.IN_PROGRESS)
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.1", "c/1.2"]


def test_a_task_can_carry_its_own_track(pl):
    p, gh = pl
    p.t.front_paths, p.t.front_implementers = ["game/*"], 1
    p.state.task("c/1.1")["track"] = "front"
    assert p.track_of("c/1.1") == "front" and p.track_of("c/1.2") == "engine"


# ------------------------------------------------------------------ full gate on the base
class _DevGit:
    def __init__(self):
        self.removed = 0

    def worktree_detached(self, path, ref): pass
    def worktree_remove(self, path): self.removed += 1
    def git(self, *args, cwd=None, check=True): return proc_mod.Result(0, "abc123 sim: monsters wait", "")


def test_the_dev_gate_runs_when_the_base_moved_and_its_interval_passed(pl):
    p, gh = pl
    p.t.dev_gate = "check -Full"
    p.current_base = lambda: "base2"
    p.pool.submit = lambda fn, *a: _NotDone()
    p.dev_gate_path.parent.mkdir(parents=True, exist_ok=True)
    p.dev_gate_path.write_text('{"base": "base2", "at": 0}')
    p.check_dev_gate()
    assert "devgate" not in p.jobs  # this base already ran
    p.dev_gate_path.write_text(f'{{"base": "base1", "at": {time.time()}}}')
    p.check_dev_gate()
    assert "devgate" not in p.jobs  # too soon
    p.dev_gate_path.write_text('{"base": "base1", "at": 0}')
    p.check_dev_gate()
    assert p.jobs["devgate"].kind == "dev gate"


def test_a_failing_dev_gate_files_one_approved_front_ticket_then_comments_on_it(pl):
    p, gh = pl
    p.t.dev_gate, p.t.dev_gate_note = "check -Full", "Draw fixed runs."
    p.git = _DevGit()
    p.dev_gate_path.parent.mkdir(parents=True, exist_ok=True)
    p.dev_gate_path.write_text('{"last_ok": "base0"}')
    p.run_gate = lambda cwd, slot, cmd, timeout: proc_mod.Result(1, "test godot_project failed", "")
    p.job_dev_gate("base1")
    n = json.loads(p.dev_gate_path.read_text())["ticket"]
    assert S.PENDING_IMPLEMENTATION.label in gh.issues[n]["labels"]
    key = f"ticket/{n}"
    assert p.state.task(key)["track"] == "front" and key in p.state.data["triaged"]
    assert "godot_project failed" in gh.issues[n]["body"] and "monsters wait" in gh.issues[n]["body"]
    assert "Draw fixed runs." in gh.issues[n]["body"]
    p.job_dev_gate("base2")
    assert len([i for i in gh.issues.values() if "full gate fails" in i["title"]]) == 1
    assert "base2"[:8] in gh.comments_by[n][-1]["body"]
    p.run_gate = lambda cwd, slot, cmd, timeout: proc_mod.Result(0, "", "")
    p.job_dev_gate("base3")
    assert "passes" in gh.comments_by[n][-1]["body"]
    assert json.loads(p.dev_gate_path.read_text())["last_ok"] == "base3"


def test_a_priority_entry_can_name_a_change_and_a_ticket_that_writes_it(pl):
    from agentpipe import milestones
    from agentpipe.tasks import Task
    p, gh = pl
    p.t.priority = ["c"]
    assert p.rank_of("c/1.1") == milestones.PRIORITY_RANK
    p.tasks["ticket/9"] = Task("ticket", "9", "plan c", False, "improvement", 0)
    assert p.rank_of("ticket/9") != milestones.PRIORITY_RANK
    p.t.priority = ["new-change"]
    p.state.task("ticket/9")["origin"] = "new-change"
    assert p.rank_of("ticket/9") == milestones.PRIORITY_RANK
    assert p.rank_of("c/1.1") != milestones.PRIORITY_RANK


def test_exclusive_work_runs_alone_until_it_is_done(pl):
    from agentpipe.tasks import Task
    p, gh = pl
    p.t.exclusive = ["godot-thin-tests"]
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    p.inbox_dir.mkdir(parents=True, exist_ok=True)
    (p.inbox_dir / "1.json").write_text('{"title": "Plan it", "origin": "godot-thin-tests"}')
    p.take_inbox = lambda: None  # still waiting in the inbox
    p.tick()
    assert p.started == [] and p.exclusive_on
    (p.inbox_dir / "1.json").unlink()
    n = gh.add(TICKET_MARKER, S.PENDING_IMPLEMENTATION)
    p.tasks[f"ticket/{n}"] = Task("ticket", str(n), "Plan it", False, "improvement", 0)
    p.state.task(f"ticket/{n}")["origin"] = "godot-thin-tests"
    p.issues_by_key = lambda raw: {("c/1.1" if i["number"] == 1 else f"ticket/{i['number']}"): i for i in raw}
    p.tick()
    assert [s[1] for s in p.started] == [f"ticket/{n}"]
    gh.issues[n]["state"] = "CLOSED"
    p.tick()
    assert "c/1.1" in [s[1] for s in p.started] and not p.exclusive_on


def test_an_exclusive_ticket_holds_the_rest_while_github_has_not_listed_it_yet(pl):
    p, gh = pl
    p.t.exclusive = ["godot-thin-tests"]
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    p.inbox_dir.mkdir(parents=True, exist_ok=True)
    (p.inbox_dir / "1.json").write_text('{"title": "Plan it", "body": "b", "origin": "godot-thin-tests"}')
    listed = gh.list_issues
    gh.list_issues = lambda state="all": [i for i in listed(state) if "Plan it" not in i["title"]]  # not listed yet
    p.tick()
    assert p.started == [] and p.exclusive_on
    gh.list_issues = listed
    p.tick()
    assert [s[1] for s in p.started] == ["ticket/2"]
    gh.issues[2]["state"] = "CLOSED"
    p.tick()
    assert "c/1.1" in [s[1] for s in p.started]


def test_a_dispatcher_that_handed_over_does_not_take_back_a_target_stopped_on_request(pl, monkeypatch):
    from agentpipe import service
    p, gh = pl
    started = _code_pipeline(p, monkeypatch, ["a" * 40, "b" * 40])
    alive = {777: True}
    monkeypatch.setattr(service, "is_alive", lambda pid: pid == os.getpid() or alive.get(pid, False))
    lines, ticks = [], []
    p._log = lines.append
    job = type("J", (), {"kind": "implement", "slot": "impl-0", "issue": 5})()

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            p.jobs["c/1.1"] = job
        elif len(ticks) == 2:
            assert p.handing_off
            p.stopped_file.write_text("777", encoding="utf-8")  # the successor stopped on request
            alive[777] = False
        elif len(ticks) == 4:
            assert started == ["t"] and p.handing_off  # not taken back, nothing restarted
            p.jobs.clear()
        assert len(ticks) < 10

    def start(name, wd, cfg, handed_over=False):
        started.append(name)
        return 777

    monkeypatch.setattr(service, "start", start)
    p.tick = tick
    p.run_forever()
    assert any("stopped on request; finishing 1 job(s) here, then exiting" in l for l in lines)
    assert not any("taking the target back" in l for l in lines)
    assert "finished the jobs it handed over" in lines[-1]


def test_exclusive_work_does_not_wait_for_files_held_by_work_the_hold_keeps_back(pl):
    p, gh = pl
    p.t.exclusive = ["c/1.2"]
    touching(p, **{"1_1": ["game/tests/run_setup.gd"], "1_2": ["game/tests/run_setup.gd"]})
    task_issue(gh, "c/1.1", S.CHANGES_REQUESTED)  # has a PR on the same file, held back
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    gh.prs = [{"number": 20, "headRefOid": "s", "body": TASK_MARKER.format(key="c/1.1"), "files": []}]
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.2"]


def test_what_exclusive_work_waits_for_runs_too(pl):
    from agentpipe.tasks import Task
    p, gh = pl
    p.t.exclusive = ["plan-x"]
    task_issue(gh, "c/1.1", S.IN_PROGRESS)  # named by the ticket
    task_issue(gh, "c/1.2", S.IN_PROGRESS)  # unrelated: held back
    n = gh.add(TICKET_MARKER + "\nNeeds c/1.1 first.", S.PENDING_IMPLEMENTATION)
    key = f"ticket/{n}"
    p.tasks[key] = Task("ticket", str(n), "Plan x", False, "improvement", 0)
    p.state.task(key)["origin"] = "plan-x"
    p.issues_by_key = lambda raw: {(f"c/1.{i['number']}" if i["number"] < 3 else f"ticket/{i['number']}"): i
                                   for i in raw}
    p.load_tickets = lambda issues: p.ticket_bodies.update({key: gh.issues[n]["body"]})
    p.ticket_bodies = {key: gh.issues[n]["body"]}
    p.register_wait = lambda waiter, blockers, old=None: None
    p.tick()
    started = [s[1] for s in p.started]
    assert "c/1.1" in started and "c/1.2" not in started


def test_when_all_exclusive_work_waits_for_max_the_rest_runs(pl):
    from agentpipe.tasks import Task
    p, gh = pl
    p.t.exclusive = ["plan-x"]
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    n = gh.add(TICKET_MARKER, S.NEEDS_DECISION)
    p.tasks[f"ticket/{n}"] = Task("ticket", str(n), "Plan x", False, "improvement", 0)
    p.state.task(f"ticket/{n}")["origin"] = "plan-x"
    p.issues_by_key = lambda raw: {("c/1.1" if i["number"] == 1 else f"ticket/{i['number']}"): i for i in raw}
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.1"] and not p.exclusive_on


def test_a_filed_exclusive_ticket_never_listed_is_looked_up_after_a_while(pl):
    p, gh = pl
    p.t.exclusive = ["plan-x"]
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    p.state.data["exclusive_tickets"] = {"99": {"seen": False, "at": time.time()}}
    gh.issue = lambda n: {"number": n, "state": "CLOSED", "body": "", "labels": []}
    p.tick()
    assert p.started == []  # too soon to look it up
    p.state.data["exclusive_tickets"]["99"]["at"] = time.time() - 3600
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.1"] and not p.state.data["exclusive_tickets"]


def test_an_exclusive_change_name_covers_the_changes_own_tasks(pl):
    p, gh = pl
    p.t.exclusive = ["c"]
    assert p.is_exclusive("c/1.1")
    p.t.exclusive = ["other"]
    assert not p.is_exclusive("c/1.1")


def test_no_second_agent_session_starts_in_a_worktree_whose_session_still_runs(pl):
    # arpg #673/#675: a handed-over dispatcher started an implementer beside a live one.
    from agentpipe import handoff
    p, gh = pl
    t = p.tasks["c/1.1"]
    calls = []
    p.clean_workspace_crates = lambda *a: None
    p.resumable = lambda *a: None
    p.claude = type("C", (), {"run": lambda self, *a, **k: calls.append(a)})()
    with handoff.held_lock(p.session_lock(t)):  # the other session
        with pytest.raises(handoff.Busy):
            p.run_agent(t, "go", "impl-0", "impl", "m")
    assert calls == []


def test_a_busy_worktree_is_not_counted_as_a_failure(pl):
    from agentpipe import handoff
    p, gh = pl
    issue = task_issue(gh, "c/1.1", S.IN_PROGRESS)
    fut = type("F", (), {"done": lambda self: True, "exception": lambda self: handoff.Busy("x")})()
    p.jobs["c/1.1"] = type("J", (), {"kind": "implement", "future": fut})()
    p.reap()
    assert "fail_implement" not in p.state.task("c/1.1") and S.IN_PROGRESS.label in gh.issues[issue]["labels"]


def test_the_session_lock_frees_when_its_holder_dies(tmp_path):
    import subprocess, sys
    from agentpipe import handoff
    lock = tmp_path / "locks" / "x.lock"
    code = ("import sys,time; from pathlib import Path; from agentpipe import handoff\n"
            "with handoff.held_lock(Path(sys.argv[1])):\n print('held', flush=True); time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(handoff.Busy):
            with handoff.held_lock(lock):
                pass
    finally:
        child.kill()
        child.wait()
    with handoff.held_lock(lock):
        pass
