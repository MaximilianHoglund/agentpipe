"""Model tiers: light tasks and runs go to the cheaper model, and escalate when they struggle."""

import time

from agentpipe import claude as claude_mod
from agentpipe.claude import ClaudeResult
from agentpipe.config import Agent
from agentpipe.deps import Plan, TaskDeps, validate_plan
from agentpipe.tasks import parse_tasks
from test_pipeline import pl  # noqa: F401  (pytest fixture)

FULL, LIGHT = "opus", "sonnet"


class ModelClaude:
    """Answers each run with the next queued subtype and records which model ran it."""

    def __init__(self, *subtypes):
        self.subtypes, self.calls = list(subtypes), []

    def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
        self.calls.append((agent.model, log_name, prompt, resume))
        sub = self.subtypes.pop(0) if self.subtypes else "success"
        ok = sub == "success"
        return ClaudeResult(ok, f"sess-{len(self.calls)}", "", {"status": "done", "summary": "", "tests": ""}
                            if ok else None, False, subtype=sub)


class MergeGit:
    def __init__(self, merging_after=(True, False)):
        self.states = list(merging_after)

    def merging(self, wt):
        return self.states.pop(0) if self.states else False

    def is_clean(self, wt):
        return True


def tiered(p, light=("c/1.1",)):
    p.cfg.agents["implementer"] = Agent(FULL, 1, [], light_model=LIGHT)
    p.plan = Plan("fp", {k: TaskDeps(tier="light" if k in light else "standard") for k in p.tasks})
    return p


def test_models_by_run_and_tier(pl):
    p = tiered(pl[0])
    light, std = p.tasks["c/1.1"], p.tasks["c/1.2"]
    assert p.model_for(light, "impl") == LIGHT and p.model_for(std, "impl") == FULL
    assert p.model_for(std, "conflict") == LIGHT and p.model_for(std, "cleanup") == LIGHT
    assert p.model_for(light, "revise") == FULL
    p.escalate(light, "test")
    assert p.model_for(light, "impl") == FULL and p.model_for(light, "conflict") == FULL


def test_unrated_tasks_and_configs_without_a_light_model_run_full(pl):
    p = tiered(pl[0])
    p.plan.tasks["c/1.1"].tier = None
    assert p.model_for(p.tasks["c/1.1"], "impl") == FULL
    p.cfg.agents["implementer"] = Agent(FULL, 1, [])
    assert p.model_for(p.tasks["c/1.2"], "conflict") == FULL


def test_a_batch_is_light_only_if_every_member_is(pl):
    p = tiered(pl[0], light=("c/1.1",))
    p.state.task("c/1.1")["batch"] = ["c/1.1", "c/1.2"]
    assert p.model_for(p.tasks["c/1.1"], "impl") == FULL


def test_capped_light_run_escalates_and_is_redone_on_the_full_model(pl):
    p = tiered(pl[0])
    t = p.tasks["c/1.1"]
    p.claude = ModelClaude(claude_mod.MAX_TURNS, "success")
    res = p.agent(t, "first", "impl-0", "impl", retry_prompt=lambda: "fresh")
    assert res.ok
    assert [(m, pr, r) for m, _, pr, r in p.claude.calls] == [(LIGHT, "first", None), (FULL, "fresh", None)]
    st = p.state.task("c/1.1")
    assert "turn cap" in st["escalated"] and st["session_model"] == FULL


def test_capped_full_run_is_not_retried(pl):
    p = tiered(pl[0])
    p.claude = ModelClaude(claude_mod.MAX_TURNS)
    res = p.agent(p.tasks["c/1.2"], "x", "impl-0", "impl")
    assert res.subtype == claude_mod.MAX_TURNS and len(p.claude.calls) == 1
    assert "escalated" not in p.state.task("c/1.2")


def test_unresolved_light_conflict_is_retried_on_the_full_model(pl):
    p = tiered(pl[0])
    p.git = MergeGit(merging_after=(True, False))
    p.claude = ModelClaude("success", "success")
    p.resolve_conflict(p.tasks["c/1.2"], "impl-0")
    assert [c[0] for c in p.claude.calls] == [LIGHT, FULL]
    assert "unresolved" in p.state.task("c/1.2")["escalated"]


def test_resolved_light_conflict_needs_no_retry(pl):
    p = tiered(pl[0])
    p.git = MergeGit(merging_after=(False,))
    p.claude = ModelClaude("success")
    p.resolve_conflict(p.tasks["c/1.2"], "impl-0")
    assert [c[0] for c in p.claude.calls] == [LIGHT]


def test_a_session_is_resumed_only_by_its_own_model(pl):
    p = tiered(pl[0])
    t = p.tasks["c/1.1"]
    p.state.task("c/1.1").update(session="s", session_at=time.time(), session_model=LIGHT)
    assert p.resumable(t, "impl") == "s"
    assert p.resumable(t, "revise") is None


def test_planner_tier_is_kept_and_unknown_tiers_dropped():
    tasks = parse_tasks("a", "- [ ] 1.1 x\n- [ ] 1.2 y\n- [ ] 1.3 z\n")
    plan = validate_plan(tasks, {"tasks": [
        {"key": "a/1.1", "blocked_by": [], "autonomous": True, "tier": "light"},
        {"key": "a/1.2", "blocked_by": [], "autonomous": True, "tier": "Standard"},
        {"key": "a/1.3", "blocked_by": [], "autonomous": True, "tier": "huge"}]}, "fp")
    assert [plan.get(k).tier for k in ("a/1.1", "a/1.2", "a/1.3")] == ["light", "standard", None]


def test_backfill_rates_unrated_tasks_on_the_light_planner(pl):
    p, _ = pl
    p.cfg.agents["planner"] = Agent(FULL, 1, [], light_model=LIGHT)
    p.plan.tasks["c/1.1"].touches = ["a.rs"]
    p.plan.tasks["c/1.1"].tier = None
    for k in ("c/1.2", "c/1.3", "c/1.4"):
        p.plan.tasks[k].touches, p.plan.tasks[k].tier = ["b.rs"], "standard"
    seen = {}

    class C:
        def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
            seen.update(model=agent.model, prompt=prompt)
            return ClaudeResult(True, "s", "", {"tasks": [{"key": "c/1.1", "touches": ["z.rs"], "tier": "light"}]},
                                False, subtype="success")

    p.claude = C()
    assert p.backfill_touches(list(p.tasks.values()), p.plan)
    assert seen["model"] == LIGHT and "c/1.1 |" in seen["prompt"] and "c/1.2 |" not in seen["prompt"]
    assert p.plan.tasks["c/1.1"].tier == "light" and p.plan.tasks["c/1.1"].touches == ["a.rs"]


# ------------------------------------------------------------------ tickets
def ticket_pl(p):
    from agentpipe.status import TICKET_MARKER, Status
    from agentpipe.tasks import Task
    p.cfg.agents["implementer"] = Agent(FULL, 1, [], light_model=LIGHT)
    p.cfg.agents["planner"] = Agent(FULL, 1, [], light_model=LIGHT)
    issues = {}
    for n, s in ((7, Status.PENDING_IMPLEMENTATION), (8, Status.PENDING_APPROVAL), (9, Status.PENDING_IMPLEMENTATION)):
        k = f"ticket/{n}"
        issues[k] = {"number": n, "state": "OPEN", "labels": [s.label], "body": f"{TICKET_MARKER}\nbody {n}"}
        p.tasks[k] = Task("ticket", str(n), f"title {n}", False, "defect", 0)
    p.ticket_bodies = {k: i["body"] for k, i in issues.items()}
    p.state.task("ticket/9")["runs"] = 1  # already started: keeps whatever it ran on
    return issues


def test_approved_unstarted_tickets_are_rated_once_on_the_light_planner(pl):
    p, _ = pl
    issues = ticket_pl(p)
    seen = []

    class C:
        def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
            seen.append((agent.model, prompt))
            return ClaudeResult(True, "s", "", {"tickets": [{"key": "ticket/7", "tier": "light"}]}, False,
                                subtype="success")

    p.claude = C()
    p.rate_tickets(issues)
    p.rate_tickets(issues)
    assert len(seen) == 1 and seen[0][0] == LIGHT
    assert "ticket/7" in seen[0][1] and "ticket/8" not in seen[0][1] and "ticket/9" not in seen[0][1]
    assert p.state.task("ticket/7")["tier"] == "light"
    assert p.model_for(p.tasks["ticket/7"], "impl") == LIGHT
    assert p.model_for(p.tasks["ticket/9"], "impl") == FULL


def test_ticket_rating_is_skipped_without_a_light_model(pl):
    p, _ = pl
    issues = ticket_pl(p)
    p.cfg.agents["implementer"] = Agent(FULL, 1, [])
    p.claude = None  # would fail if called
    p.rate_tickets(issues)
