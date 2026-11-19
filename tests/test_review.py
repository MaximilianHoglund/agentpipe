from agentpipe import review
from agentpipe.claude import parse_output
from agentpipe.status import (Status, is_bot_comment, parse_review_marker, status_from_labels,
                              task_key_from_body)


def finding(severity="blocking", category="duplication"):
    return {"severity": severity, "category": category, "file": "src/a.rs", "line": 3,
            "problem": "p", "required_change": "r"}


def raw(verdict="approve", findings=(), probes=()):
    return {"verdict": verdict, "summary": "s", "findings": list(findings), "probes": list(probes)}


def test_blocking_finding_forces_rejection_even_if_reviewer_approved():
    assert review.normalize_verdict(raw("approve", [finding()]))["verdict"] == review.REQUEST_CHANGES


def test_nits_alone_approve_even_if_reviewer_rejected():
    assert review.normalize_verdict(raw("request_changes", [finding("nit")]))["verdict"] == review.APPROVE


def test_uncaught_probe_adds_a_blocking_coverage_finding():
    v = review.normalize_verdict(raw(probes=[{"mutation": "flip < to <=", "caught": False}]))
    assert v["verdict"] == review.REQUEST_CHANGES
    assert v["findings"][-1]["category"] == "test-coverage"
    assert "flip < to <=" in v["findings"][-1]["problem"]


def test_equivalent_or_caught_probes_do_not_block():
    probes = [{"mutation": "a", "caught": True}, {"mutation": "b", "caught": False, "equivalent": True}]
    assert review.normalize_verdict(raw(probes=probes))["verdict"] == review.APPROVE


def test_a_break_left_untested_by_design_does_not_block_and_shows_its_reason():
    probe = {"mutation": "Accuracy 300 -> 150", "caught": False,
             "untested_by_design": "docs/decisions.md:638 tests don't pin boss tuning"}
    v = review.normalize_verdict(raw(probes=[probe]))
    assert v["verdict"] == review.APPROVE
    assert "untested by design: docs/decisions.md:638" in review.render_review(v, "a" * 40, 1, 3)


def test_a_blank_untested_by_design_reason_still_blocks():
    v = review.normalize_verdict(raw(probes=[{"mutation": "a", "caught": False, "untested_by_design": " "}]))
    assert v["verdict"] == review.REQUEST_CHANGES


def test_uncaught_probe_already_explained_by_a_finding_is_not_duplicated():
    v = review.normalize_verdict(raw(findings=[finding(category="test-coverage")],
                                     probes=[{"mutation": "a", "caught": False}]))
    assert len(v["findings"]) == 1


def test_unknown_category_and_severity_are_coerced_safely():
    v = review.normalize_verdict(raw(findings=[{**finding(), "category": "vibes", "severity": "meh"}]))
    assert v["findings"][0]["category"] == "other"
    assert v["verdict"] == review.REQUEST_CHANGES  # unknown severity counts as blocking


def test_rendered_review_round_trips_its_marker_and_counts_rounds():
    v = review.normalize_verdict(raw("request_changes", [finding()]))
    body = review.render_review(v, "abc123def", 2, 3)
    assert parse_review_marker(body) == {"sha": "abc123def", "verdict": "request_changes", "round": 2}
    assert "round 2/3" in body and "`src/a.rs:3`" in body
    conflict = review.render_review({"verdict": review.CONFLICT, "summary": "", "findings": [], "probes": []},
                                    "abc", 0, 3)
    history = review.review_history([{"body": body}, {"body": "human"}, {"body": conflict}])
    assert [h["verdict"] for h in history] == ["request_changes", "conflict"]
    assert review.rounds_used(history) == 1  # conflicts don't use up a round


def test_a_conflict_after_an_approval_starts_a_fresh_round_count():
    h = [{"verdict": v} for v in ("request_changes", "request_changes", "approve", "conflict",
                                  "request_changes")]
    assert review.rounds_used(h) == 1  # the update's rejection is round 1 of a fresh count
    assert review.rounds_used(h[:3]) == 2
    # A conflict before any approval resets nothing.
    assert review.rounds_used([{"verdict": v} for v in ("request_changes", "conflict", "tests_failed")]) == 2


def test_human_comments_exclude_pipeline_comments():
    comments = [{"body": review.bot("x")}, {"body": "please use the existing parser"},
                {"body": review.render_review(raw(), "abc", 1, 3)}]
    assert [c["body"] for c in review.human_comments(comments)] == ["please use the existing parser"]
    assert not is_bot_comment(None)


def test_prompts_render_without_missing_variables():
    common = dict(key="k", text="t", test_cmd="cargo test", context_list="x", spec_ref="s",
                  open_tickets="none", project_dir=".")
    review.prompt("implementer", **common, section="s", issue=1, blockers="none", worktree="w", human_input="",
                  batch_section="", prior_work="", self_check="", base_ref="origin/main")
    review.prompt("revise", key="k", round=1, max_rounds=3, feedback="f", merge_note="", test_cmd="c",
                  self_check="", base_ref="origin/main")
    review.prompt("reviewer", **common, pr=1, head="h", base_ref="origin/main", diffstat="d",
                  pr_summary="p", mutation_section="", prior_section="", batch_section="")
    review.prompt("planner", repo_root="r", target="t", project_dir=".", context_list="c",
                  changes_list="c", open_tasks="o", done_tasks="d", existing_section="",
                  answer_rule="r", max_batch=4)
    review.prompt("preflight", shells="Bash", commands="- `x`")


def test_status_labels_and_markers():
    assert status_from_labels(["bug", Status.IN_PROGRESS.label]) is Status.IN_PROGRESS
    assert status_from_labels(["bug"]) is None
    # A human adding needs-decision on top of an agent status wins.
    assert status_from_labels([Status.IN_PROGRESS.label, Status.NEEDS_DECISION.label]) is Status.NEEDS_DECISION
    assert task_key_from_body("x <!-- pipeline-task: dota2-draft-helper/1.2 --> y") == "dota2-draft-helper/1.2"
    assert task_key_from_body(None) is None


def test_parse_output_success_failure_and_limits():
    ok = parse_output('noise\n{"type":"result","subtype":"success","is_error":false,"result":"{}",'
                      '"session_id":"s1","structured_output":{"a":1},"total_cost_usd":0.5}', "", 0)
    assert (ok.ok, ok.session_id, ok.structured, ok.limited) == (True, "s1", {"a": 1}, False)
    turns = parse_output('{"subtype":"error_max_turns","is_error":true,"result":"","session_id":"s2"}', "", 1)
    assert (turns.ok, turns.limited, turns.session_id) == (False, False, "s2")
    limit = parse_output('{"subtype":"success","is_error":true,"result":"Claude usage limit reached"}', "", 1)
    assert limit.limited and not limit.ok
    five_hour = parse_output('{"subtype":"success","is_error":true,'
                             '"result":"You\'ve hit your 5-hour limit \\u00b7 resets 11:30pm (Europe/Stockholm)"}', "", 1)
    assert five_hour.limited and not five_hour.ok
    crash = parse_output("", "Error: 529 overloaded", 1)
    assert crash.limited and not crash.ok
    no_schema = parse_output('{"subtype":"success","is_error":false,"result":"hi"}', "", 0)
    assert not no_schema.ok  # structured output is required


def test_parse_output_reads_stream_json_and_its_usage_windows():
    stream = "\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":'
        '{"five_hour":{"utilization":0.82,"resetsAt":1793797800,"status":"allowed"},'
        '"seven_day":{"utilization":0.37,"resetsAt":1794099600,"status":"allowed"}}}}',
        '{"type":"assistant","message":{"content":[]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"","session_id":"s9",'
        '"structured_output":{"a":1}}',
    ])
    res = parse_output(stream, "", 0)
    assert (res.ok, res.session_id, res.structured) == (True, "s9", {"a": 1})
    assert res.rate["five_hour"]["utilization"] == 0.82


def _probe(rule="r", brk="a.rs:1 > to >=", caught="t"):
    return {"rule": rule, "break": brk, "caught_by": caught}


def test_a_done_report_needs_three_caught_probes_or_a_reason():
    assert review.probe_gap({"status": "done", "probes": [_probe()] * 3}) is None
    gap = review.probe_gap({"status": "done", "probes": [_probe(), _probe(rule="cap", caught="")]})
    assert "1 deliberate-break probe" in gap and "no catching test yet: cap" in gap
    assert "no_probes" in review.probe_gap({"status": "done"})
    assert review.probe_gap({"status": "done", "no_probes": "docs only"}) is None
    # Only `done` goes to review; unfinished work and questions aren't held up.
    assert review.probe_gap({"status": "incomplete"}) is None
    assert review.probe_gap({"status": "needs_decision"}) is None


def test_the_pr_body_lists_the_probes_or_why_there_are_none():
    assert "caught by `t`" in review.probe_section({"probes": [_probe()]})
    assert review.probe_section({"no_probes": "docs only"}) == "none: docs only"
    assert review.probe_section({}) == "none reported"
