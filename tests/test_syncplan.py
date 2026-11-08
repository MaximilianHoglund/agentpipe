from agentpipe import syncplan
from agentpipe.deps import Plan, TaskDeps
from agentpipe.status import Status
from agentpipe.tasks import parse_tasks

S = Status


def issue(n, status=None, state="OPEN"):
    return {"number": n, "state": state, "labels": [status.label] if status else []}


def setup(text="- [x] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n", plan=None):
    tasks = parse_tasks("c", text)
    plan = plan or Plan("fp", {"c/1.2": TaskDeps(["c/1.1"]), "c/1.3": TaskDeps(["c/1.2"])})
    return tasks, plan


def test_new_tasks_get_issues_with_graph_status_and_done_tasks_none():
    tasks, plan = setup()
    actions = syncplan.compute(tasks, plan, {}, set(), set())
    assert actions == [syncplan.Create("c/1.2", S.PENDING_IMPLEMENTATION),
                       syncplan.Create("c/1.3", S.BLOCKED_BY_TASK)]


def test_closed_blocker_issue_unblocks_even_before_tasks_md_is_ticked():
    tasks, plan = setup()
    issues = {"c/1.2": issue(1, S.FINISHED, "CLOSED"), "c/1.3": issue(2, S.BLOCKED_BY_TASK)}
    assert syncplan.compute(tasks, plan, issues, set(), set()) == [
        syncplan.SetStatus("c/1.3", 2, S.BLOCKED_BY_TASK, S.PENDING_IMPLEMENTATION)]


def test_agent_and_human_owned_statuses_are_never_overwritten():
    tasks, plan = setup()
    for owned in (S.IN_PROGRESS, S.PENDING_REVIEW, S.CHANGES_REQUESTED, S.APPROVED, S.NEEDS_DECISION,
                  S.AWAITING_TASK):
        issues = {"c/1.2": issue(1, owned), "c/1.3": issue(2, S.BLOCKED_BY_TASK)}
        assert syncplan.compute(tasks, plan, issues, set(), set()) == [], owned


def test_unlabelled_open_issue_gets_a_status():
    tasks, plan = setup()
    issues = {"c/1.2": issue(1), "c/1.3": issue(2, S.BLOCKED_BY_TASK)}
    assert syncplan.compute(tasks, plan, issues, set(), set()) == [
        syncplan.SetStatus("c/1.2", 1, None, S.PENDING_IMPLEMENTATION)]


def test_hand_ticked_task_closes_its_idle_issue_but_not_one_an_agent_owns():
    tasks, plan = setup("- [x] 1.1 a\n- [x] 1.2 b\n- [ ] 1.3 c\n")
    idle = {"c/1.2": issue(1, S.PENDING_IMPLEMENTATION), "c/1.3": issue(2, S.PENDING_IMPLEMENTATION)}
    assert syncplan.compute(tasks, plan, idle, set(), set()) == [
        syncplan.Close("c/1.2", 1, S.PENDING_IMPLEMENTATION)]
    busy = {"c/1.2": issue(1, S.IN_PROGRESS), "c/1.3": issue(2, S.PENDING_IMPLEMENTATION)}
    assert syncplan.compute(tasks, plan, busy, set(), set()) == []


def test_non_autonomous_task_is_flagged_with_reason():
    tasks, _ = setup()
    plan = Plan("fp", {"c/1.2": TaskDeps(["c/1.1"], False, "needs an API key"), "c/1.3": TaskDeps()})
    actions = syncplan.compute(tasks, plan, {}, set(), set())
    assert syncplan.Create("c/1.2", S.NEEDS_DECISION, "needs an API key") in actions


def test_non_autonomous_task_waits_for_its_blockers_before_asking():
    tasks, _ = setup()
    plan = Plan("fp", {"c/1.2": TaskDeps(), "c/1.3": TaskDeps(["c/1.2"], False, "play the build")})
    assert syncplan.Create("c/1.3", S.BLOCKED_BY_TASK) in syncplan.compute(tasks, plan, {}, set(), set())
    # Flagged too early by an older pipeline: taken back, not treated as a human clearing it.
    early = {"c/1.2": issue(1, S.IN_PROGRESS), "c/1.3": issue(2, S.NEEDS_DECISION)}
    assert syncplan.compute(tasks, plan, early, {"c/1.3"}, set()) == [
        syncplan.SetStatus("c/1.3", 2, S.NEEDS_DECISION, S.BLOCKED_BY_TASK)]
    # A needs decision the pipeline didn't set (an agent asked mid-task) is left alone.
    assert syncplan.compute(tasks, plan, early, set(), set()) == []
    # Once the blocker is done, it asks.
    done = {"c/1.2": issue(1, S.FINISHED, "CLOSED"), "c/1.3": issue(2, S.BLOCKED_BY_TASK)}
    assert syncplan.compute(tasks, plan, done, set(), set()) == [
        syncplan.SetStatus("c/1.3", 2, S.BLOCKED_BY_TASK, S.NEEDS_DECISION, "play the build")]


def test_play_task_waits_for_its_release_then_asks_with_the_tag():
    tasks, _ = setup()
    plan = Plan("fp", {"c/1.2": TaskDeps(), "c/1.3": TaskDeps([], False, "play the build.")})
    play = {"c/1.3": "step-1"}
    assert syncplan.Create("c/1.3", S.BLOCKED_BY_TASK) in syncplan.compute(
        tasks, plan, {}, set(), set(), play, set())
    # Flagged before the release existed: taken back.
    early = {"c/1.2": issue(1, S.IN_PROGRESS), "c/1.3": issue(2, S.NEEDS_DECISION)}
    assert syncplan.compute(tasks, plan, early, {"c/1.3"}, set(), play, set()) == [
        syncplan.SetStatus("c/1.3", 2, S.NEEDS_DECISION, S.BLOCKED_BY_TASK)]
    blocked = {"c/1.2": issue(1, S.IN_PROGRESS), "c/1.3": issue(2, S.BLOCKED_BY_TASK)}
    [a] = syncplan.compute(tasks, plan, blocked, set(), set(), play, {"step-1"})
    assert (a.new, a.reason) == (S.NEEDS_DECISION,
                                 "play the build. Play the `step-1` release (see the repo's GitHub releases).")


def test_human_clearing_a_flag_is_respected_on_later_syncs():
    tasks, _ = setup()
    plan = Plan("fp", {"c/1.2": TaskDeps(["c/1.1"], False, "needs an API key"), "c/1.3": TaskDeps()})
    issues = {"c/1.2": issue(1, S.PENDING_IMPLEMENTATION), "c/1.3": issue(2, S.PENDING_IMPLEMENTATION)}
    # First sync after the human relabels: recorded as cleared, status left alone.
    assert syncplan.compute(tasks, plan, issues, {"c/1.2"}, set()) == [syncplan.Cleared("c/1.2")]
    # Later syncs: already cleared, still left alone.
    assert syncplan.compute(tasks, plan, issues, set(), {"c/1.2"}) == []
    # Without the clear, the planner's verdict would put it back.
    assert syncplan.compute(tasks, plan, issues, set(), set()) == [
        syncplan.SetStatus("c/1.2", 1, S.PENDING_IMPLEMENTATION, S.NEEDS_DECISION, "needs an API key")]


def test_ready_actions_come_before_blocked_and_human_ones():
    tasks = parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n")
    plan = Plan("fp", {"c/1.1": TaskDeps(["c/1.4"]), "c/1.2": TaskDeps([], False, "ask"),
                       "c/1.3": TaskDeps(), "c/1.4": TaskDeps()})
    issues = {"c/1.4": issue(9, S.BLOCKED_BY_TASK)}
    assert syncplan.compute(tasks, plan, issues, set(), set()) == [
        syncplan.Create("c/1.3", S.PENDING_IMPLEMENTATION),
        syncplan.SetStatus("c/1.4", 9, S.BLOCKED_BY_TASK, S.PENDING_IMPLEMENTATION),
        syncplan.Create("c/1.1", S.BLOCKED_BY_TASK),
        syncplan.Create("c/1.2", S.NEEDS_DECISION, "ask"),
    ]
