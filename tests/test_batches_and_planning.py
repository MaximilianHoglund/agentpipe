"""Batches, incremental planning, preflight, run caps and session resume."""

import json
import time

import pytest

from agentpipe import claude as claude_mod
from agentpipe import proc
from agentpipe.claude import Claude, ClaudeResult, parse_output
from agentpipe.config import Agent
from agentpipe.deps import (Plan, PlanError, TaskDeps, classify, merge_plans, stale_keys, text_hash,
                            validate_plan)
from agentpipe.status import TASK_MARKER, Status
from agentpipe.tasks import parse_tasks
from test_pipeline import FakeGitHub, pl  # noqa: F401  (pytest fixture)

S = Status
TASKS = parse_tasks("a", "- [x] 1.1 done\n- [ ] 1.2 two\n- [ ] 1.3 three\n- [ ] 1.4 four\n") + \
    parse_tasks("b", "- [ ] 1.1 other\n- [ ] 1.2 other two\n")


def e(key, blocked_by=(), batch="", autonomous=True, reason=""):
    return {"key": key, "blocked_by": list(blocked_by), "autonomous": autonomous,
            "human_reason": reason, "batch": batch}


# ------------------------------------------------------------------ batch validation
def test_valid_batch_and_leader_follower_classification():
    raw = {"tasks": [e("a/1.2", ["a/1.1"], "x"), e("a/1.3", ["a/1.2", "b/1.1"], "x"), e("a/1.4", ["a/1.3"]),
                     e("b/1.1"), e("b/1.2", ["b/1.1"])]}
    plan = validate_plan(TASKS, raw, "fp")
    assert plan.batch_of("a/1.3") == ["a/1.2", "a/1.3"]
    by = {t.key: t for t in TASKS}
    done = {"a/1.1"}
    # The leader waits for every member's outside blockers (b/1.1 via a/1.3); followers stay blocked.
    assert classify(by["a/1.2"], plan, done) == "blocked"
    assert classify(by["a/1.3"], plan, done) == "blocked"
    assert classify(by["a/1.2"], plan, done | {"b/1.1"}) == "ready"
    assert classify(by["a/1.3"], plan, done | {"b/1.1"}) == "blocked"
    # Once the batch merges, its dependents are free.
    assert classify(by["a/1.4"], plan, done | {"b/1.1", "a/1.2", "a/1.3"}) == "ready"


@pytest.mark.parametrize("raw, problem", [
    ({"tasks": [e("a/1.2", batch="x"), e("a/1.3", batch="x"), e("a/1.4", batch="x"), e("b/1.1"), e("b/1.2")]},
     "at most 2"),
    ({"tasks": [e("a/1.2", batch="x"), e("b/1.1", batch="x"), e("a/1.3"), e("a/1.4"), e("b/1.2")]},
     "mixes changes"),
    ({"tasks": [e("a/1.2", batch="x"), e("a/1.3", batch="x", autonomous=False, reason="r"), e("a/1.4"),
                e("b/1.1"), e("b/1.2")]}, "non-autonomous"),
    # a/1.2 -> b/1.1 -> a/1.3, with a/1.2 and a/1.3 batched: a cycle once collapsed.
    ({"tasks": [e("a/1.2", batch="x"), e("a/1.3", ["b/1.1"], "x"), e("b/1.1", ["a/1.2"]), e("a/1.4"),
                e("b/1.2")]}, "cycle"),
])
def test_bad_batches_are_rejected(raw, problem):
    with pytest.raises(PlanError, match=problem):
        validate_plan(TASKS, raw, "fp", max_batch=2)


def test_single_member_batch_dissolves_and_max_batch_one_disables():
    raw = {"tasks": [e("a/1.2", batch="x"), e("a/1.3", batch="y"), e("a/1.4", batch="y"), e("b/1.1"), e("b/1.2")]}
    assert validate_plan(TASKS, raw, "fp").get("a/1.2").batch == ""
    assert validate_plan(TASKS, raw, "fp", max_batch=1).get("a/1.3").batch == ""


# ------------------------------------------------------------------ incremental planning
def plan_of(tasks, raw):
    return validate_plan(tasks, raw, "old")


BASE_RAW = {"tasks": [e("a/1.2", batch="x"), e("a/1.3", ["a/1.2"], "x"), e("a/1.4"), e("b/1.1"), e("b/1.2")]}


def test_stale_keys_finds_new_reworded_and_their_batches():
    old = plan_of(TASKS, BASE_RAW)
    tasks = parse_tasks("a", "- [x] 1.1 done\n- [ ] 1.2 two\n- [ ] 1.3 THREE reworded\n- [ ] 1.4 four\n") + \
        parse_tasks("b", "- [ ] 1.1 other\n- [ ] 1.3 new\n")
    stale, removed = stale_keys(tasks, old)
    assert stale == {"a/1.3", "a/1.2", "b/1.3"}  # a/1.2 rides along with its batch
    assert removed == {"b/1.2"}


def test_merge_plans_keeps_untouched_entries_and_drops_removed():
    old = plan_of(TASKS, {"tasks": [e("a/1.2"), e("a/1.3"), e("a/1.4", ["b/1.2"]), e("b/1.1"), e("b/1.2")]})
    merged = merge_plans(old, {"tasks": [e("b/1.3", ["a/1.4"])]}, {"b/1.3"}, {"b/1.2"})
    by = {x["key"]: x for x in merged["tasks"]}
    assert "b/1.2" not in by and by["a/1.4"]["blocked_by"] == []
    assert by["b/1.3"]["blocked_by"] == ["a/1.4"]


def test_old_plan_files_load_and_save_texts(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"fingerprint": "f", "tasks": {"a/1.2": {"blocked_by": [], "autonomous": True,
                                                                         "human_reason": ""}}}))
    p = Plan.load(path)
    assert p.get("a/1.2").batch == "" and p.texts == {}
    p.texts = {"a/1.2": "h"}
    p.save(path)
    assert Plan.load(path).texts == {"a/1.2": "h"}


class FakeClaude:
    """Stands in for Claude: records prompts, answers with queued structured outputs."""

    def __init__(self, *answers, subtype="success", denials=()):
        self.answers, self.prompts, self.subtype, self.denials = list(answers), [], subtype, list(denials)

    def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
        self.prompts.append((log_name, prompt, resume))
        ok = self.subtype == "success"
        return ClaudeResult(ok, "sess-1", "", self.answers.pop(0) if ok and self.answers else None,
                            False, subtype=self.subtype, denials=self.denials)


def test_make_plan_sends_only_stale_tasks_with_the_rest_as_context(pl):
    p, gh = pl
    old = plan_of(TASKS, BASE_RAW)
    tasks = TASKS + parse_tasks("b", "- [ ] 1.3 brand new\n")
    p.claude = FakeClaude({"tasks": [e("b/1.3", ["b/1.2"])]})
    p.log = lambda m: None
    plan = p.make_plan(tasks, "fp2", old)
    _, prompt, _ = p.claude.prompts[0]
    todo = prompt.split("## Open tasks to plan")[1].split("Done:")[0]
    assert "b/1.3 |" in todo and "a/1.2 |" not in todo
    assert "## Already planned" in prompt and "a/1.2 | " in prompt.split("## Already planned")[1]
    assert plan.get("b/1.3").blocked_by == ["b/1.2"] and plan.get("a/1.3").batch == "x"
    assert plan.texts["b/1.3"] == text_hash(tasks[-1])


def test_make_plan_without_stale_tasks_needs_no_planner(pl):
    p, gh = pl
    old = plan_of(TASKS, BASE_RAW)
    p.claude = FakeClaude()
    plan = p.make_plan([t for t in TASKS if t.key != "b/1.2"], "fp3", old)
    assert p.claude.prompts == [] and "b/1.2" not in plan.tasks


# ------------------------------------------------------------------ preflight
def test_failed_preflight_starts_no_agents_and_reports_why(pl, monkeypatch):
    p, gh = pl
    p.t.preflight = ["cargo --version"]
    p.env_ok, p.last_preflight = False, 0.0
    p.refresh_base = lambda: None
    lines = []
    p.log = lines.append
    monkeypatch.setattr("agentpipe.pipeline.proc.run",
                        lambda *a, **k: proc.Result(1, "", "'cargo' is not recognized"))
    p.claude = FakeClaude({"results": [{"tool": "Bash", "command": "cargo --version", "exit_code": 0}]},
                          denials=[{"tool_name": "PowerShell", "tool_input": {"command": "cargo --version"}}])
    p.cfg.agents["preflight"] = Agent("h", 5, ["Bash", "PowerShell"])
    gh.add(TASK_MARKER.format(key="c/1.1"), S.PENDING_IMPLEMENTATION)
    p.tick()
    assert p.started == []
    text = "\n".join(lines)
    assert "preflight FAILED" in text and "dispatcher's shell" in text and "denied" in text
    assert "did not report PowerShell: cargo --version" in text


def test_passing_preflight_lets_agents_start(pl, monkeypatch):
    p, gh = pl
    p.t.preflight = ["cargo --version"]
    p.env_ok, p.last_preflight = False, 0.0
    p.refresh_base = lambda: None
    p.log = lambda m: None
    monkeypatch.setattr("agentpipe.pipeline.proc.run", lambda *a, **k: proc.Result(0, "cargo 1.x", ""))
    p.claude = FakeClaude({"results": [{"tool": s, "command": "cargo --version", "exit_code": 0}
                                       for s in ("Bash", "PowerShell")]})
    p.cfg.agents["preflight"] = Agent("h", 5, ["Bash", "PowerShell"])
    gh.add(TASK_MARKER.format(key="c/1.1"), S.PENDING_IMPLEMENTATION)
    p.tick()
    assert p.env_ok and p.started == [("implement", "c/1.1", "impl-0")]


# ------------------------------------------------------------------ caps and resume
def test_timeout_is_a_capped_subtype():
    assert parse_output("", "timed out after 5s", proc.TIMEOUT_CODE).subtype == claude_mod.TIMEOUT


def test_capped_run_goes_to_decision_and_forgets_its_session(pl):
    p, gh = pl
    n = gh.add(TASK_MARKER.format(key="c/1.1"), S.IN_PROGRESS)
    t = p.tasks["c/1.1"]
    p.state.task("c/1.1")["session"] = "s"
    p.log = lambda m: None
    p.deliver(t, n, None, ClaudeResult(False, "s", "", None, False, subtype=claude_mod.MAX_TURNS),
              "impl-0", S.IN_PROGRESS)
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]
    assert "1-turn cap" in gh.comments_by[n][-1]["body"]
    assert "session" not in p.state.task("c/1.1")


def test_only_recent_sessions_are_resumed(pl):
    p, gh = pl
    t = p.tasks["c/1.1"]
    st = p.state.task("c/1.1")
    st.update(session="s", session_at=time.time() - 60)
    assert p.resumable(t) == "s"
    st["session_at"] = time.time() - p.cfg.resume_within_seconds - 1
    assert p.resumable(t) is None


# ------------------------------------------------------------------ batch jobs
class FakeGit:
    def __init__(self):
        self.pushed = []

    def is_clean(self, wt): return True
    def merging(self, wt): return False
    def commits_ahead(self, wt, ref): return 1
    def fetch(self): pass
    def contains(self, wt, ref): return True
    def push(self, wt, branch): self.pushed.append(branch)


def test_batch_leader_records_followers_and_its_pr_closes_them_all(pl):
    p, gh = pl
    p.plan = Plan("fp", {"c/1.1": TaskDeps(batch="b"), "c/1.2": TaskDeps(["c/1.1"], batch="b"),
                         "c/1.3": TaskDeps(batch="b"), "c/1.4": TaskDeps()})
    lead = gh.add(TASK_MARKER.format(key="c/1.1"), S.IN_PROGRESS)
    f2 = gh.add(TASK_MARKER.format(key="c/1.2"), S.BLOCKED_BY_TASK)
    gh.add(TASK_MARKER.format(key="c/1.3"), S.IN_PROGRESS)  # already claimed: stays out of the batch
    p.issue_map = {k: i for k, i in p.issues_by_key(gh.list_issues()).items()}
    p.log = lambda m: None
    p.record_batch(p.tasks["c/1.1"])
    assert [x.key for x in p.batch(p.tasks["c/1.1"])] == ["c/1.1", "c/1.2"]
    assert "Batched" in gh.comments_by[f2][-1]["body"]
    assert "c/1.2" in p.batch_section(p.tasks["c/1.1"])
    created = {}
    gh.create_pr = lambda branch, base, title, body: created.update(title=title, body=body) or 99
    p.git = FakeGit()
    p.deliver(p.tasks["c/1.1"], lead, None,
              ClaudeResult(True, "s", "", {"status": "done", "summary": "sum", "tests": "ok"}, False),
              "impl-0", S.IN_PROGRESS)
    assert f"Closes #{lead}" in created["body"] and f"Closes #{f2}" in created["body"]
    assert created["title"].startswith("c 1.1, 1.2:")
