import pytest

from agentpipe.deps import Plan, PlanError, classify, find_cycle, id_sort_key, validate_plan
from agentpipe.tasks import parse_tasks

TASKS = parse_tasks("a", "- [x] 1.1 done\n- [ ] 1.2 two\n- [ ] 1.3 three\n") + \
    parse_tasks("b", "- [ ] 1.1 other change\n")


def entry(key, blocked_by=(), autonomous=True, reason=""):
    return {"key": key, "blocked_by": list(blocked_by), "autonomous": autonomous, "human_reason": reason}


def valid_raw():
    return {"tasks": [entry("a/1.2", ["a/1.1"]), entry("a/1.3", ["a/1.2"]), entry("b/1.1", ["a/1.3"])]}


def test_valid_plan_keeps_cross_change_blockers():
    plan = validate_plan(TASKS, valid_raw(), "fp")
    assert plan.get("b/1.1").blocked_by == ["a/1.3"]


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["tasks"].pop(), "missing from plan: b/1.1"),
    (lambda r: r["tasks"].append(entry("a/9.9")), "unknown task 'a/9.9'"),
    (lambda r: r["tasks"].append(entry("a/1.2")), "a/1.2 listed twice"),
    (lambda r: r["tasks"][0]["blocked_by"].append("a/1.2"), "a/1.2 blocks itself"),
    (lambda r: r["tasks"][0]["blocked_by"].append("zz/1"), "unknown task 'zz/1'"),
    (lambda r: r["tasks"][0].update(autonomous=False), "without a human_reason"),
    (lambda r: r["tasks"][0]["blocked_by"].append("b/1.1"), "dependency cycle"),
])
def test_invalid_plans_are_rejected(mutate, message):
    raw = valid_raw()
    mutate(raw)
    with pytest.raises(PlanError, match=message.replace("(", r"\(").replace(")", r"\)")):
        validate_plan(TASKS, raw, "fp")


def test_done_tasks_may_be_omitted_and_duplicate_blockers_collapse():
    raw = valid_raw()
    raw["tasks"][0]["blocked_by"] = ["a/1.1", "a/1.1"]
    assert validate_plan(TASKS, raw, "fp").get("a/1.2").blocked_by == ["a/1.1"]


def test_find_cycle_returns_closed_path_or_none():
    assert find_cycle({"x": ["y"], "y": ["x"]}) in (["x", "y", "x"], ["y", "x", "y"])
    assert find_cycle({"x": ["x"]}) == ["x", "x"]
    assert find_cycle({"x": ["y"], "y": ["z"]}) is None  # z has no entry: a leaf
    assert find_cycle({}) is None


def test_classify():
    plan = validate_plan(TASKS, valid_raw(), "fp")
    t12, t13 = TASKS[1], TASKS[2]
    assert classify(t12, plan, {"a/1.1"}) == "ready"
    assert classify(t13, plan, {"a/1.1"}) == "blocked"
    assert classify(t13, plan, {"a/1.1", "a/1.2"}) == "ready"
    human = Plan("fp", {**plan.tasks, "a/1.2": type(plan.get("a/1.2"))(["a/1.1"], False, "needs key")})
    assert classify(t12, human, {"a/1.1"}) == "needs-human"


def test_id_sort_is_numeric_with_suffix():
    ids = ["2.4b", "1.10", "2.1", "1.2", "2.4a"]
    assert sorted(ids, key=id_sort_key) == ["1.2", "1.10", "2.1", "2.4a", "2.4b"]


def test_plan_round_trips_through_disk(tmp_path):
    plan = validate_plan(TASKS, valid_raw(), "fp")
    plan.save(tmp_path / "p.json")
    assert Plan.load(tmp_path / "p.json") == plan
    assert Plan.load(tmp_path / "missing.json") is None


def test_touches_are_normalized_kept_through_save_and_unknown_until_predicted(tmp_path):
    from agentpipe.deps import Plan, validate_plan
    from agentpipe.tasks import parse_tasks
    tasks = parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.2 b\n")
    plan = validate_plan(tasks, {"tasks": [
        {"key": "c/1.1", "blocked_by": [], "autonomous": True, "touches": ["./src\\run.rs", "src/run.rs", " "]},
        {"key": "c/1.2", "blocked_by": [], "autonomous": True}]}, "fp")
    assert plan.get("c/1.1").touches == ["src/run.rs"]
    assert plan.get("c/1.2").touches is None
    plan.save(tmp_path / "plan.json")
    again = Plan.load(tmp_path / "plan.json")
    assert again.get("c/1.1").touches == ["src/run.rs"] and again.get("c/1.2").touches is None
