import json

from agentpipe import denials, proc, watch
from agentpipe.claude import Claude, parse_output, safe_name
from agentpipe.config import Agent

DENY = ["Bash(git push:*)", "Bash(git config --global:*)", "PowerShell(Remove-Item:*)", "WebFetch"]


def test_matching_rule_checks_each_part_and_whole_words_only():
    assert denials.matching_rule("Bash", "echo hi && git push origin x", DENY) == "Bash(git push:*)"
    assert denials.matching_rule("Bash", "git  config --global user.name x", DENY) == "Bash(git config --global:*)"
    assert denials.matching_rule("Bash", "git pushy", DENY) is None
    assert denials.matching_rule("PowerShell", "remove-item d -Recurse", DENY) == "PowerShell(Remove-Item:*)"
    assert denials.matching_rule("WebFetch", "https://x", DENY) == "WebFetch"
    assert denials.matching_rule("Bash", "cargo --version", DENY) is None


def _out(path, items):
    path.write_text("noise\n" + json.dumps({"subtype": "success", "permission_denials": items}) + "\n", encoding="utf-8")


def test_scan_and_report_separate_gaps_from_deny_rules(tmp_path):
    _out(tmp_path / "a-impl-1.out.json", [
        {"tool_name": "Bash", "tool_input": {"command": "git push"}},
        {"tool_name": "PowerShell", "tool_input": {"command": "cargo --version"}},
        {"tool_name": "PowerShell", "tool_input": {"command": "cargo --version; ls"}}])
    _out(tmp_path / "b-review-x-0.out.json", [])
    found = denials.scan(tmp_path, DENY)
    assert [(d.run, d.reason) for d in found] == [
        ("a-impl-1", "deny rule Bash(git push:*)"), ("a-impl-1", "not allowed"), ("a-impl-1", "not allowed")]
    text = denials.report(found)
    assert text.index("NOT ALLOWED") < text.index("DENY RULES")
    assert "2x  PowerShell: cargo --version" in text
    assert "[Bash(git push:*)]" in text
    assert denials.report([]) == "No denied tool calls."
    later = denials.scan(tmp_path, DENY, allowed={"PowerShell"})
    assert [d.reason for d in later][1:] == ["no longer denied", "no longer denied"]


def test_claude_run_reports_denials_and_writes_plain_log_names(tmp_path):
    result = {"subtype": "success", "result": "", "structured_output": {},
              "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]}
    seen = []
    c = Claude("claude", tmp_path, 60, run=lambda cmd, cwd, **kw: proc.Result(0, json.dumps(result), ""),
               on_denials=lambda name, ds: seen.append((name, ds)))
    res = c.run("p", tmp_path, Agent("m", 1, []), {}, log_name="plan-abc:x,y-0")
    assert res.denials == result["permission_denials"]
    assert seen == [("plan-abc_x_y-0", result["permission_denials"])]
    assert (tmp_path / "plan-abc_x_y-0.out.json").exists()
    assert safe_name("a/b:c d") == "a_b_c_d"
    assert parse_output("{}", "", 0).denials == []


def test_watch_marks_denied_calls_apart_from_errors():
    lines = [
        json.dumps({"type": "assistant", "timestamp": "2026-11-03T12:00:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "git push"}}]}}),
        json.dumps({"type": "user", "timestamp": "2026-11-03T12:00:01Z", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": "Permission to use Bash with command git push has been denied."}]}}),
    ]
    tr = watch.parse_transcript(lines)
    assert (tr.denied, tr.errors) == (1, 0)
    assert tr.events[-1].kind == "denied" and tr.events[-1].text == "DENIED Bash: git push"
