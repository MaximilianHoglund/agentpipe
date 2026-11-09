"""Unfinished implementer runs requeue themselves, requeued work keeps its claim on its files,
and work is merged with the base (and gated) before it goes to review."""

from agentpipe import pipeline as pipeline_mod
from agentpipe import proc
from agentpipe.claude import ClaudeResult
from agentpipe.status import TASK_MARKER, Status
from test_pipeline import pl, task_issue, touching  # noqa: F401  (pytest fixture)

S = Status


def done(summary="sum", status="done"):
    return ClaudeResult(True, "s", "", {"status": status, "summary": summary, "tests": "t", "docs": "none"}, False)


class SyncGit:
    """A worktree whose work is committed; records restores."""

    def __init__(self, merging=False):
        self.in_merge, self.restored, self.pushed, self.commits = merging, 0, [], []

    def fetch(self): pass
    def head(self, wt): return "h0"
    def is_clean(self, wt): return True
    def merging(self, wt): return self.in_merge
    def commits_ahead(self, wt, ref): return 1
    def contains(self, wt, ref): return True
    def push(self, wt, branch): self.pushed.append(branch)
    def restore(self, wt): self.restored += 1
    def merge_abort(self, wt): self.in_merge = False

    def commit_all(self, wt, message):
        self.commits.append(message)
        return True


def with_issue(p, gh, status=S.IN_PROGRESS):
    n = gh.add(TASK_MARKER.format(key="c/1.1"), status)
    t = p.tasks["c/1.1"]
    (p.wt(t) / ".git").mkdir(parents=True, exist_ok=True)
    p.git, p.log = SyncGit(), (lambda m: None)
    return n, t


# ------------------------------------------------------------------ unfinished runs
def test_an_unfinished_run_is_saved_and_requeued_with_its_notes(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.state.task("c/1.1")["session"] = "s"
    p.deliver(t, n, None, done("Seeds left: find a QUICK_DEATH seed", "incomplete"), "impl-0", S.IN_PROGRESS)
    assert S.IN_PROGRESS.label in gh.issues[n]["labels"]  # not parked with Max
    st = p.state.task("c/1.1")
    assert "session" not in st and st["unfinished_runs"] == 1
    assert p.git.commits and "stopped unfinished" in p.git.commits[0] and p.git.pushed
    assert "requeued" in gh.comments_by[n][-1]["body"]
    assert "QUICK_DEATH" in p.prior_work(t, None)


def test_unfinished_runs_go_to_max_only_after_the_requeues_run_out(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    for _ in range(pipeline_mod.UNFINISHED_REQUEUES):
        p.deliver(t, n, None, done("left", "incomplete"), "impl-0", S.IN_PROGRESS)
        assert S.IN_PROGRESS.label in gh.issues[n]["labels"]
    p.deliver(t, n, None, done("still left", "incomplete"), "impl-0", S.IN_PROGRESS)
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]
    assert "still left" in gh.comments_by[n][-1]["body"]


def test_a_delivery_forgets_earlier_unfinished_runs(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.deliver(t, n, None, done("left", "incomplete"), "impl-0", S.IN_PROGRESS)
    gh.create_pr = lambda *a: 7
    p.deliver(t, n, None, done(), "impl-0", S.IN_PROGRESS)
    st = p.state.task("c/1.1")
    assert "unfinished" not in st and "unfinished_runs" not in st
    assert S.PENDING_REVIEW.label in gh.issues[n]["labels"]


# ------------------------------------------------------------------ claim on files
def test_requeued_work_with_a_pr_is_not_held_back_by_overlapping_work(pl):
    p, gh = pl
    touching(p, **{"1_1": ["src/run.rs"], "1_2": ["src/run.rs"]})
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)  # back from needs decision, has a PR
    task_issue(gh, "c/1.2", S.IN_PROGRESS)  # started on the same files meanwhile
    gh.prs = [{"number": 20, "headRefOid": "s", "body": TASK_MARKER.format(key="c/1.1"), "files": []}]
    p.tick()
    assert "c/1.1" in [s[1] for s in p.started] and "c/1.1" not in p.held


def test_requeued_work_that_ran_before_is_not_held_back(pl):
    p, gh = pl
    touching(p, **{"1_1": ["src/run.rs"], "1_2": ["src/run.rs"]})
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    p.state.task("c/1.2")["runs"] = 3
    p.tick()
    assert {s[1] for s in p.started} == {"c/1.1", "c/1.2"}


def test_a_fresh_task_still_waits_for_overlapping_work(pl):
    p, gh = pl
    touching(p, **{"1_1": ["src/run.rs"], "1_2": ["src/run.rs"]})
    task_issue(gh, "c/1.1", S.IN_PROGRESS)
    task_issue(gh, "c/1.2", S.PENDING_IMPLEMENTATION)
    p.tick()
    assert [s[1] for s in p.started] == ["c/1.1"] and "c/1.2" in p.held


# ------------------------------------------------------------------ merging the base at handoff
def syncing(p, merge, gates, runs=()):
    """Stub the merge result, the gate results in order, and the agent's answers."""
    p.merge_base = lambda wt, slot, gate: merge
    gates = list(gates)
    p.gates = 0

    def gate(cwd, slot, cmd, timeout):
        p.gates += 1
        return proc.Result(0 if gates.pop(0) else 1, "test x failed", "")
    p.run_gate = gate
    runs = list(runs)
    p.agent_calls = []

    def agent(t, prompt, slot, tag, retry_prompt=None):
        p.agent_calls.append((tag, prompt))
        return runs.pop(0) if runs else done()
    p.agent = agent
    return p


def test_an_up_to_date_branch_is_handed_over_without_another_gate(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    syncing(p, "current", [])
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) == {"summary": "x"}
    assert p.gates == 0 and p.agent_calls == []


def test_a_clean_merge_that_passes_the_gate_needs_no_agent(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    syncing(p, "clean", [True])
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is not None
    assert p.gates == 1 and p.agent_calls == []


def test_a_gate_that_fails_once_then_passes_is_flaky_not_the_agents_job(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    syncing(p, "clean", [False, True])
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is not None
    assert p.gates == 2 and p.agent_calls == []


def test_a_merge_that_breaks_the_gate_goes_back_to_the_implementers_session(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    syncing(p, "clean", [False, False, True], runs=[done("adapted to the base")])
    out = p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x", "tests": "t"})
    assert [c[0] for c in p.agent_calls] == ["sync"] and "test x failed" in p.agent_calls[0][1]
    assert out["summary"] == "adapted to the base"
    assert p.model_for(t, "sync") == p.model_for(t, "mutants")  # the session's own model


def test_a_gate_still_failing_after_the_fixes_goes_to_review_anyway(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    syncing(p, "clean", [False] * 10)
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is not None
    assert len(p.agent_calls) == pipeline_mod.SYNC_FIXES
    assert S.IN_PROGRESS.label in gh.issues[n]["labels"]


def test_a_conflict_at_handoff_is_resolved_by_the_implementers_session(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    syncing(p, "conflict", [])
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is not None
    assert [c[0] for c in p.agent_calls] == ["sync"] and p.gates == 0


def test_an_unresolved_conflict_at_handoff_goes_to_a_decision(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.git.in_merge = True
    syncing(p, "conflict", [], runs=[ClaudeResult(False, "s", "", None, False, subtype="error")])
    p.resolve_conflict = lambda t, slot: ClaudeResult(False, "s", "", None, False, subtype="error")
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is None
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]


def test_a_conflict_run_runs_the_quick_check_and_leaves_the_full_gate_to_review(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.t.quick_check = "cargo clippy --all-targets"
    prompt = p.conflict_prompt(t)
    assert "run `cargo clippy --all-targets`" in prompt
    assert f"Don't run the full `{p.t.test}`" in prompt
    p.t.quick_check = ""
    assert "only the tests that cover the code you resolved" in p.conflict_prompt(t)


def test_with_a_quick_check_an_up_to_date_branch_is_gated_and_its_head_recorded(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.t.quick_check = "cargo clippy"
    p.git.head = lambda wt: "h1"
    syncing(p, "current", [True])
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is not None
    assert p.gates == 1 and p.state.task("c/1.1")["handoff_gated"] == "h1"


def test_with_a_quick_check_a_resolved_conflict_is_gated_before_review(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.t.quick_check = "cargo clippy"
    p.git.head = lambda wt: "h1"
    syncing(p, "conflict", [True])
    assert p.sync_with_base(t, n, S.IN_PROGRESS, "impl-0", {"summary": "x"}) is not None
    assert [c[0] for c in p.agent_calls] == ["sync"] and p.gates == 1


def test_agents_are_told_the_quick_check_not_the_gate_when_there_is_one(pl):
    p, gh = pl
    assert p.agent_check() == f"`{p.t.test}`"
    p.t.quick_check = "cargo clippy"
    assert p.agent_check().startswith("`cargo clippy` and the tests")
    assert f"Don't run the full `{p.t.test}`" in p.agent_check()


def test_the_review_skips_its_gate_only_on_the_head_gated_at_handoff(pl):
    p, gh = pl
    n, t = with_issue(p, gh)
    p.state.task("c/1.1")["handoff_gated"] = "h1"
    p.git.head = lambda wt: "h1"
    assert p.handoff_gated(t, p.wt(t), "h1")
    assert not p.handoff_gated(t, p.wt(t), "h2")  # pushed since
    p.git.head = lambda wt: "merged"  # the review had to merge a newer base into it
    assert not p.handoff_gated(t, p.wt(t), "h1")
