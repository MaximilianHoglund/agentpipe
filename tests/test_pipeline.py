"""Scheduler and ticket handling against an in-memory GitHub; jobs are recorded, not run."""

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
    task_issue(gh, "c/1.1", S.PENDING_APPROVAL)
    task_issue(gh, "c/1.2", S.PENDING_APPROVAL)
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


def test_status_without_pr_goes_to_needs_decision_but_fresh_tickets_wait(pl):
    p, gh = pl
    n = task_issue(gh, "c/1.1", S.PENDING_APPROVAL)
    t = gh.add(TICKET_MARKER + "\nfix it", S.PENDING_APPROVAL, title="Dedupe parser")
    p.state.data["tickets"] = [t]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.tick()
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]
    assert S.PENDING_APPROVAL.label in gh.issues[t]["labels"]  # proposal waits for a human
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


def test_file_tickets_creates_pending_approval_issues_and_skips_duplicates(pl):
    p, gh = pl
    p.file_tickets("the review of PR #3", [
        {"kind": "defect", "title": "Off-by-one in pager", "body": "pager.rs:10"},
        {"kind": "bogus", "title": "Unify config loading", "body": "x"},
        {"kind": "defect", "title": "off-by-one in pager ", "body": "dup, different case"},
        {"kind": "defect", "title": "  ", "body": "no title"},
    ])
    created = list(gh.issues.values())
    assert [i["title"] for i in created] == ["Off-by-one in pager", "Unify config loading"]
    assert all(S.PENDING_APPROVAL.label in i["labels"] for i in created)
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


def test_a_replan_that_reverses_an_edge_relinks_without_a_cycle(tmp_path):
    from agentpipe.deps import TaskDeps
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
