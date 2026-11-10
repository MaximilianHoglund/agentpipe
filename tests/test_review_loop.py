"""Main-loop and state-store robustness: concurrent saves, the ticket swap in a sync, ticks that
raise, reviews whose label never followed, and work that goes on through a usage pause."""

import json
import sys
import threading
import time

import pytest

from agentpipe import proc as proc_mod
from agentpipe.deps import Plan, PlanError, TaskDeps
from agentpipe.pipeline import LimitHit, State
from agentpipe.status import TASK_MARKER, TICKET_MARKER, Status
from agentpipe.tasks import Task, TaskParseError, parse_tasks
from test_pipeline import _Job, pl, task_issue  # noqa: F401  (pl is a pytest fixture)

S = Status


# ---------------------------------------------------------------- 1: saving under mutation
def test_saving_state_survives_job_threads_changing_task_dicts(tmp_path):
    st = State(tmp_path / "state.json")
    for i in range(4):  # big dicts, so a save is inside one of them most of the time
        st.task(f"c/{i}").update({f"f{j}": j for j in range(1500)}, runs=1)
    stop, errors = threading.Event(), []

    held = [st.task(f"c/{i}") for i in range(4)]  # as a job holds `st = self.state.task(key)`

    def mutate():  # what job threads do: add and drop keys of those dicts, without the lock
        n = 0
        while not stop.is_set():
            n += 1
            for d in held:
                d[f"k{n % 50}"] = n
                d.pop(f"k{(n + 25) % 50}", None)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # switch threads often: the race shows within a few saves
    threads = [threading.Thread(target=mutate, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for _ in range(10):
            try:
                st.save()
            except RuntimeError as e:  # "dictionary changed size during iteration"
                errors.append(e)
                break
    finally:
        stop.set()
        for t in threads:
            t.join()
        sys.setswitchinterval(interval)
    assert not errors
    disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(disk["tasks"]) == 4 and disk["tasks"]["c/0"]["runs"] == 1


def test_state_json_stays_indented_for_people(tmp_path):
    st = State(tmp_path / "state.json")
    st.task("c/1")["runs"] = 2
    st.save()
    assert '\n    "c/1": {\n      "runs": 2\n' in (tmp_path / "state.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 2: tickets through a sync
def syncing(p, deps=None):
    """Let ``p.sync`` run for real against the fake GitHub: no git, no planner, no links."""
    p.last_sync = 0
    p.refresh_base = lambda: None
    p.sync_links = lambda gh, issues: None
    p.backfill_touches = lambda tasks, plan: False
    if deps is not None:
        tasks = [t for k, t in p.tasks.items() if not k.startswith("ticket/")]
        p.load_tasks = lambda: tasks
        p.make_plan = lambda tasks, fp, old=None: Plan(fp, deps)


BLOCKED_FIRST = {"c/1.1": TaskDeps(["c/1.4"]), "c/1.2": TaskDeps(), "c/1.3": TaskDeps(), "c/1.4": TaskDeps()}


def test_a_sync_never_shows_the_scheduler_the_tasks_without_the_open_tickets(pl):
    p, gh = pl
    p.t.exclusive = ["plan-x"]
    n = gh.add(TICKET_MARKER + "\nthe spec", S.PENDING_IMPLEMENTATION, title="Plan x")
    key = f"ticket/{n}"
    p.state.data["tickets"] = [n]
    p.state.task(key)["origin"] = "plan-x"
    p.state.data["exclusive_tickets"] = {str(n): {"seen": True, "at": time.time()}}
    p.load_tickets(p.issues_by_key(gh.list_issues()))  # as the previous sync left it
    # c/1.1 is blocked, so its issue comes after the ready ones: the sync schedules in between.
    syncing(p, BLOCKED_FIRST)
    schedule, seen = p.schedule, []

    def watching(**kw):
        seen.append((key in p.tasks, p.ticket_bodies.get(key), len(gh.issues)))
        schedule(**kw)

    p.schedule = watching
    p.tick()
    assert seen[0] == (True, TICKET_MARKER + "\nthe spec", 4)  # mid-sync: c/1.1 has no issue yet
    assert len(seen) == 2 and len(gh.issues) == 5
    # The hold stood through the sync: only the exclusive ticket started, and it is still held for.
    assert [s[1] for s in p.started] == [key]
    assert str(n) in p.state.data["exclusive_tickets"] and p.exclusive_on


def test_the_first_sync_after_a_start_loads_tickets_before_it_schedules(pl):
    p, gh = pl
    p.t.exclusive = ["plan-x"]
    n = gh.add(TICKET_MARKER, S.PENDING_IMPLEMENTATION, title="Plan x")
    p.state.data["tickets"] = [n]
    p.state.task(f"ticket/{n}")["origin"] = "plan-x"
    p.state.data["exclusive_tickets"] = {str(n): {"seen": True, "at": time.time()}}
    syncing(p, BLOCKED_FIRST)
    p.tasks = {}  # a dispatcher that just started
    p.tick()
    assert [s[1] for s in p.started] == [f"ticket/{n}"]
    assert str(n) in p.state.data["exclusive_tickets"]


def test_loading_tickets_drops_closed_ones_and_keeps_one_github_does_not_list_yet(pl):
    p, gh = pl
    a = gh.add(TICKET_MARKER + "\na", S.PROPOSED, title="A")
    b = gh.add(TICKET_MARKER + "\nb", S.PROPOSED, title="B")
    p.state.data["tickets"] = [a, b]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    assert {"ticket/1", "ticket/2", "c/1.1"} <= set(p.tasks)
    c = p.file_ticket({"title": "C", "kind": "defect", "body": "c"}, "footer", S.PROPOSED, "a test")
    gh.issues[a]["state"] = "CLOSED"
    listing = [i for i in gh.list_issues() if i["number"] != c]  # GitHub lags behind the filing
    p.load_tickets(p.issues_by_key(listing))
    assert "ticket/1" not in p.tasks and "ticket/1" not in p.ticket_bodies
    assert p.tasks["ticket/2"].text == "B" and p.tasks[f"ticket/{c}"].text == "C"
    assert "c" in p.ticket_bodies[f"ticket/{c}"]
    assert p.file_ticket({"title": "C", "kind": "defect", "body": "again"}, "f", S.PROPOSED, "a test") is None


def test_tasks_and_ticket_bodies_are_replaced_not_changed_under_a_reader(pl):
    p, gh = pl
    tasks, bodies = p.tasks, p.ticket_bodies
    n = p.file_ticket({"title": "New", "kind": "defect", "body": "b"}, "footer", S.PROPOSED, "a test")
    assert f"ticket/{n}" in p.tasks and f"ticket/{n}" in p.ticket_bodies
    assert f"ticket/{n}" not in tasks and bodies == {}  # a thread iterating the old dicts is safe
    tasks = p.tasks
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    assert p.tasks is not tasks and f"ticket/{n}" in p.tasks


# ---------------------------------------------------------------- 3: a tick that raises
class Planner:
    """Stands in for ``Claude``: counts runs and answers as told."""

    def __init__(self):
        self.runs, self.answer = 0, None

    def run(self, prompt, cwd, agent, schema, **kw):
        from agentpipe.claude import ClaudeResult
        self.runs += 1
        if self.answer is None:
            return ClaudeResult(False, None, "boom", None, False, subtype="error")
        return ClaudeResult(True, None, "", self.answer, False)


FOUR = "- [ ] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n"


def tasks_md(p, text):
    (p.base_wt / "openspec" / "changes" / "c" / "tasks.md").write_text(text)


def planned(p, text):
    """A plan.json for the fixture's four tasks, made before tasks.md became ``text``."""
    from agentpipe.deps import text_hash
    plan = Plan("made-earlier", {k: TaskDeps(touches=[], tier="standard") for k in p.tasks},
                {k: text_hash(t) for k, t in p.tasks.items()})
    plan.save(p.plan_path)
    tasks_md(p, text)
    p.claude = Planner()
    syncing(p)
    return p.claude


def test_a_failed_replan_keeps_the_previous_plan_and_is_not_retried_every_tick(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    planner = planned(p, FOUR + "- [ ] 1.5 new\n")
    disk = p.plan_path.read_text(encoding="utf-8")
    p.tick()
    assert planner.runs == 2  # the planner's two tries
    assert "c/1.5" not in p.tasks and len(gh.issues) == 4  # known tasks synced; the new one waits
    assert ("implement", "c/1.1", "impl-0") in p.started  # known work flows
    assert p.plan_path.read_text(encoding="utf-8") == disk
    for _ in range(3):  # anything that asks for a sync at once (a merge, a handoff ending)
        p.last_sync = 0
        p.tick()
    assert planner.runs == 2
    assert sum("planning failed" in l for l in lines) == 1 and "Traceback" in "".join(lines)
    p.plan_failed = (p.plan_failed[0], time.time() - p.cfg.sync_seconds - 1, p.plan_failed[2])
    p.last_sync = 0
    p.tick()
    assert planner.runs == 4  # once per sync interval
    assert sum("planning failed" in l for l in lines) == 1  # the same error: not logged again
    # tasks.md changes again: tried at once, and this time the planner answers.
    tasks_md(p, FOUR + "- [ ] 1.5 newer\n")
    planner.answer = {"tasks": [{"key": "c/1.5", "blocked_by": []}]}
    p.last_sync = 0
    p.tick()
    assert planner.runs == 5 and "c/1.5" in p.tasks and len(gh.issues) == 5
    assert "c/1.5" in Plan.load(p.plan_path).tasks and p.plan_failed is None
    assert any("plan works again" in l for l in lines)


def test_a_failed_first_plan_does_not_stop_the_loop_or_rerun_the_planner_each_tick(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    p.claude = Planner()
    syncing(p)
    p.tasks = {}
    for _ in range(4):
        p.tick()  # raises nothing
    assert p.claude.runs == 2 and p.started == [] and not p.tasks
    assert sum("sync failed" in l and "planner failed twice" in l for l in lines) == 1


def test_a_tasks_md_that_does_not_parse_leaves_known_work_running(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    syncing(p)
    tasks_md(p, "- [ ] 1.1 a\n- [ ] 1.1 again\n")
    p.tick()
    assert p.started == [("implement", "c/1.1", "impl-0")]
    p.last_sync = 0
    p.tick()
    assert sum("sync failed" in l and "TaskParseError" in l for l in lines) == 1
    tasks_md(p, FOUR)
    p.make_plan = lambda tasks, fp, old=None: Plan(fp, {t.key: TaskDeps() for t in tasks})
    p.tick()
    assert any("sync works again" in l for l in lines)


def test_a_github_refusal_in_a_sync_is_logged_once_and_a_network_error_still_ends_the_tick(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    syncing(p, {k: TaskDeps() for k in p.tasks})

    def refuse(gh_, a, issues):
        raise proc_mod.CommandError(["gh", "issue", "create"], proc_mod.Result(1, "", "HTTP 403: secondary rate limit"))

    p.apply = refuse
    p.tick()
    p.last_sync = 0
    p.tick()
    assert sum("sync failed" in l and "HTTP 403" in l for l in lines) == 1
    assert ("implement", "c/1.1", "impl-0") in p.started

    def unreachable(gh_, a, issues):
        raise proc_mod.CommandError(["gh", "issue", "create"], proc_mod.Result(1, "", "dial tcp: i/o timeout"))

    p.apply = unreachable
    p.last_sync = 0
    with pytest.raises(proc_mod.CommandError):  # run_forever logs "GitHub unreachable", as before
        p.tick()


def test_an_error_that_keeps_coming_is_said_again_after_half_an_hour_with_how_long(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    for _ in range(5):
        p.note_error("tick", ValueError("bad plan.json"), "tick failed")
    assert len(lines) == 1 and "ValueError: bad plan.json" in lines[0]
    p.errors["tick"]["since"] -= 1900
    p.errors["tick"]["logged"] -= 1900
    p.note_error("tick", ValueError("bad plan.json"), "tick failed")
    p.note_error("tick", ValueError("bad plan.json"), "tick failed")
    assert len(lines) == 2 and "STILL FAILING after 32 min (6 times)" in lines[1]
    p.note_error("tick", ValueError("another"), "tick failed")  # a new message: logged at once
    assert len(lines) == 3 and "another" in lines[2]
    p.note_ok("tick")
    assert "tick works again" in lines[3] and not p.errors


def test_the_loop_survives_any_error_in_a_tick_and_ctrl_c_still_stops_it(pl):
    p, gh = pl
    p.cfg.poll_seconds = 0
    lines, ticks = [], []
    p._log = lines.append

    def tick():
        ticks.append(1)
        if len(ticks) <= 3:
            raise PlanError("planner failed twice: no")
        if len(ticks) == 4:
            raise TaskParseError("c: task 1.1 appears on lines 1 and 2")
        if len(ticks) == 6:
            raise KeyboardInterrupt

    p.tick = tick
    p.run_forever()
    assert len(ticks) == 6
    assert sum("planner failed twice" in l for l in lines) == 1  # three ticks, one line
    assert sum("appears on lines" in l for l in lines) == 1  # the message changed: logged
    assert any("tick works again" in l for l in lines)
    assert "stopping: waiting for running agents" in lines[-1] and not p.pid_file.exists()


def test_a_limit_hit_in_a_sync_still_pauses(pl):
    p, gh = pl
    syncing(p)

    def limited():
        raise LimitHit("5-hour limit hit")

    p.load_tasks = limited
    p.tick()
    assert p.state.data["paused_until"] > time.time() and not p.errors


# ---------------------------------------------------------------- 4: a review without its relabel
def reviewed_pr(p, gh, verdicts, label_at="2026-11-08T00:20:00Z"):
    """c/1.1 in pending review since ``label_at``; its PR has a review per verdict, the last one
    of the current head."""
    n = task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    gh.prs = [{"number": 11, "headRefOid": "sha1", "body": TASK_MARKER.format(key="c/1.1")}]
    gh.comments_by[11] = [
        {"body": f"<!-- pipeline-review sha=sha{0 if i < len(verdicts) - 1 else 1} verdict={v} round={i + 1} -->",
         "created_at": f"2026-11-08T00:2{i + 1}:00Z"} for i, v in enumerate(verdicts)]
    gh.label_times = {(n, S.PENDING_REVIEW.label): label_at}
    return n


def status(gh, n):
    return [l for l in gh.issues[n]["labels"] if l.startswith("status: ")]


def test_a_posted_approval_whose_label_never_followed_is_applied_and_merges(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    n = reviewed_pr(p, gh, ["request_changes", "approve"])
    p.state.task("c/1.1")["gated"] = {"head": "sha0", "base": "old"}
    before = time.time()
    p.tick()
    assert status(gh, n) == [S.APPROVED.label] and p.started == []
    st = p.state.task("c/1.1")
    assert st["approved_at"] >= before and st["gated"] is None  # no gate is known for it
    assert sum("was posted but the issue was never relabelled" in l for l in lines) == 1
    p.tick()
    assert p.started == [("merge", "c/1.1", "merge")]


def test_a_posted_rejection_goes_back_to_the_implementer_or_to_max_when_rounds_are_used_up(pl):
    p, gh = pl
    p.cfg.max_rounds = 3
    n = reviewed_pr(p, gh, ["request_changes", "tests_failed"])
    p.tick()
    assert status(gh, n) == [S.CHANGES_REQUESTED.label]
    p.tick()
    assert p.started == [("revise", "c/1.1", "impl-0")]

    p.jobs.clear()
    gh.set_status(n, S.PENDING_REVIEW, None)
    p.reviewed.clear()
    gh.comments_by[11] = [{"body": f"<!-- pipeline-review sha=sha{i // 2} verdict=request_changes round={i + 1} -->",
                           "created_at": f"2026-11-08T00:2{i + 1}:00Z"} for i in range(3)]
    p.tick()
    assert status(gh, n) == [S.NEEDS_DECISION.label]
    assert "rejected PR #11 3 times" in gh.comments_by[n][-1]["body"]


def test_rounds_before_a_reset_do_not_count_when_a_posted_rejection_is_applied(pl):
    from agentpipe import review
    p, gh = pl
    p.cfg.max_rounds = 2
    n = reviewed_pr(p, gh, ["request_changes", "request_changes"])
    gh.comments_by[11].insert(1, {"body": review.ROUNDS_RESET_MARKER, "created_at": "2026-11-08T00:21:30Z"})
    p.tick()
    assert status(gh, n) == [S.CHANGES_REQUESTED.label]  # one counted round of two


def test_a_posted_conflict_verdict_sends_the_pr_to_its_update_ahead_of_the_queue(pl):
    p, gh = pl
    n = reviewed_pr(p, gh, ["approve", "conflict"])
    p.tick()
    assert status(gh, n) == [S.CHANGES_REQUESTED.label]
    assert p.state.task("c/1.1")["conflict_update"] > 0 and "approved_at" not in p.state.task("c/1.1")


def test_a_label_set_after_the_review_still_means_review_again(pl):
    p, gh = pl
    n = reviewed_pr(p, gh, ["approve"], label_at="2026-11-08T09:00:00Z")
    p.tick()
    assert p.started == [("review", "c/1.1", "review-0")] and status(gh, n) == [S.PENDING_REVIEW.label]


def test_a_review_this_dispatcher_posted_is_judged_by_githubs_times_not_its_own_clock(pl):
    p, gh = pl
    n = reviewed_pr(p, gh, ["approve"], label_at="2026-11-08T00:20:30Z")
    p.reviewed[11], p.review_at[11] = "sha1", "2026-11-08T00:25:00Z"  # as job_review left them
    gh.comments_by[11][0]["created_at"] = "2026-11-08T00:20:00Z"  # GitHub: posted before the label
    p.tick()
    assert p.started == [("review", "c/1.1", "review-0")] and status(gh, n) == [S.PENDING_REVIEW.label]


def test_the_review_job_and_the_recovery_share_one_way_to_apply_a_verdict(pl):
    p, gh = pl
    n = task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    p.apply_verdict("c/1.1", n, 11, "approve", 1, {"head": "h", "base": "b"})
    assert status(gh, n) == [S.APPROVED.label] and p.state.task("c/1.1")["gated"] == {"head": "h", "base": "b"}
    m = task_issue(gh, "c/1.2", S.PENDING_REVIEW)
    p.apply_verdict("c/1.2", m, 12, "tests_failed", p.cfg.max_rounds, None)
    assert status(gh, m) == [S.NEEDS_DECISION.label]


# ---------------------------------------------------------------- 5: a usage pause
def test_a_usage_pause_still_merges_but_starts_no_agent(pl):
    p, gh = pl
    p.state.data["paused_until"] = time.time() + 3600
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    task_issue(gh, "c/1.2", S.PENDING_REVIEW)
    task_issue(gh, "c/1.3", S.APPROVED)
    task_issue(gh, "c/1.4", S.CHANGES_REQUESTED)
    gh.prs = [{"number": 10 + i, "headRefOid": f"sha{i}", "body": TASK_MARKER.format(key=f"c/1.{i}")}
              for i in (2, 3, 4)]
    p.tick()
    assert p.started == [("merge", "c/1.3", "merge")]
    p.state.data["paused_until"] = 0.0
    p.tick()
    assert {s[0] for s in p.started} == {"merge", "implement", "revise", "review"}


def test_a_paused_sync_reconciles_github_and_runs_the_gates_without_any_agent(pl):
    p, gh = pl
    lines, called = [], []
    p._log = lines.append
    p.state.data["paused_until"] = time.time() + 3600
    p.t.dev_gate = "check --full"
    p.check_dev_gate = lambda: called.append("dev gate")
    p.check_environment = lambda: called.append("preflight")
    p.triage_tickets = lambda gh, issues: called.append("triage")
    p.rate_tickets = lambda issues: called.append("rate")
    # tasks.md gained a task while paused: it needs the planner, which waits.
    planner = planned(p, "- [ ] 1.1 a\n- [x] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n- [ ] 1.5 new\n")
    p.backfill_touches = lambda tasks, plan: called.append("touches")
    done = task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)  # ticked by hand meanwhile
    p.release_waiters = lambda n: None
    gh.close_issue = lambda n: gh.issues[n].update(state="CLOSED")
    p.tick()
    p.last_sync = 0
    p.tick()
    assert planner.runs == 0 and called == ["dev gate", "dev gate"] and p.started == []
    assert gh.issues[done]["state"] == "CLOSED" and len(gh.issues) == 4  # synced; c/1.5 waits for its plan
    assert sum("planned once the usage pause ends" in l for l in lines) == 1 and not p.errors
    p.state.data["paused_until"] = 0.0
    planner.answer = {"tasks": [{"key": "c/1.5", "blocked_by": []}]}
    p.last_sync = 0
    p.tick()
    assert planner.runs == 1 and "c/1.5" in p.tasks
    assert called == ["dev gate", "dev gate", "preflight", "touches", "triage", "rate", "dev gate"]


def test_a_review_set_back_by_hand_is_not_logged_every_tick_of_a_pause(pl):
    p, gh = pl
    lines = []
    p._log = lines.append
    reviewed_pr(p, gh, ["approve"], label_at="2026-11-08T09:00:00Z")
    p.state.data["paused_until"] = time.time() + 3600
    for _ in range(3):
        p.tick()
    assert p.started == [] and sum("reviewing it again" in l for l in lines) == 1
