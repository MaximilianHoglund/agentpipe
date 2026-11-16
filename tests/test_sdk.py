"""The Agent SDK runner: the delete guard, turn wrap-up notes, and a session against a fake CLI."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agentpipe import proc
from agentpipe.claude import (Claude, RunState, checkout_root, delete_guard, delete_targets,
                              parse_output, sdk_run)
from agentpipe.config import Agent


@pytest.fixture
def wt(tmp_path):
    root = tmp_path / "wt"
    (root / "proj").mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere")  # a worktree's .git is a file
    return root


def test_checkout_root_finds_the_worktree_from_its_project_dir(wt):
    assert checkout_root(wt / "proj") == wt
    assert checkout_root(wt.parent) == wt.parent  # no checkout: cwd itself


@pytest.mark.parametrize("tool,command", [
    ("Bash", "rm -rf target/tmp"),
    ("Bash", "rm -f ./a.txt b.txt && cargo test"),
    ("Bash", "rm -rf ../proj/x"),  # back into the checkout
    ("Bash", 'rm -rf "$TMPDIR/x"'),  # unknowable: left to the prompt
    ("Bash", "git rm -r --cached x"),  # not a delete of files on disk outside
    ("PowerShell", "Remove-Item -Recurse -Force target\\tmp"),
    ("PowerShell", "Remove-Item -Path scratch -ErrorAction SilentlyContinue"),
    ("Bash", "echo rm -rf /"),
])
def test_deletes_inside_the_checkout_pass(wt, tool, command):
    assert delete_guard(command, tool, wt / "proj", wt) is None


@pytest.mark.parametrize("tool,command", [
    ("Bash", "rm -rf ../../other"),
    ("Bash", "cd .. && cd .. && rm -rf other"),
    ("Bash", "cargo clean; rm -rf ~/.cargo/registry"),
    ("Bash", "rm -rf -- /tmp/x"),
    ("PowerShell", "Remove-Item -Recurse -Force ..\\..\\other"),
    ("PowerShell", "Set-Location ..\\..; rd other"),
    ("PowerShell", "Remove-Item -LiteralPath C:\\Windows\\Temp\\x"),
])
def test_deletes_outside_the_checkout_are_denied(wt, tool, command):
    reason = delete_guard(command, tool, wt / "proj", wt)
    assert reason and "outside your checkout" in reason


def test_git_bash_drive_paths_resolve_to_windows_paths(wt):
    if os.name != "nt":
        pytest.skip("Git Bash paths exist only on Windows")
    letter = str(wt)[0].lower()
    inside = f"/{letter}/{wt.relative_to(wt.anchor).as_posix()}/proj/x"
    assert delete_targets(f"rm -rf {inside}", "Bash", wt / "proj") == [wt / "proj" / "x"]
    assert delete_guard(f"rm -rf {inside}", "Bash", wt / "proj", wt) is None


def test_wrap_up_note_goes_out_once_then_a_last_call(wt):
    s = RunState(wt, wt, cap=20, wrap_up=5)
    notes = []
    for i in range(20):
        s.see({"type": "assistant", "message": {"id": f"m{i}"}})
        s.see({"type": "assistant", "message": {"id": f"m{i}"}})  # one message, two content blocks
        s.see({"type": "assistant", "parent_tool_use_id": "t", "message": {"id": f"sub{i}"}})  # a subagent's
        notes.append(s.note())
    assert s.turns == 20
    sent = [(i + 1, n) for i, n in enumerate(notes) if n]
    assert [t for t, _ in sent] == [15, 18]
    assert "15 of your 20 turns" in sent[0][1] and "incomplete" in sent[0][1]
    assert "2 turns left" in sent[1][1]
    assert RunState(wt, wt, cap=20, wrap_up=0).note() is None


def test_hook_denials_reach_the_result(wt):
    s = RunState(wt / "proj", wt, cap=10, wrap_up=0)
    pre = s.hooks()["PreToolUse"][0].hooks[0]
    out = asyncio.run(pre({"tool_name": "Bash", "tool_input": {"command": "rm -rf ../../x"},
                           "cwd": str(wt / "proj")}, "tu1", {"signal": None}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(pre({"tool_name": "Bash", "tool_input": {"command": "rm -rf x"},
                            "cwd": str(wt / "proj")}, "tu2", {"signal": None})) == {}
    res = s.see({"type": "result", "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "gh pr"}}]})
    assert [d.get("agentpipe_hook") for d in res["permission_denials"]] == [None, "delete outside checkout"]
    # The CLI lists the hook's denial itself: tagged, not listed twice (seen live 2026-11-15).
    res = s.see({"type": "result", "permission_denials": [{"tool_name": "Bash", "tool_use_id": "tu1"}]})
    assert [d.get("agentpipe_hook") for d in res["permission_denials"]] == ["delete outside checkout"]


def _report_hook(s):
    return next(m.hooks[0] for m in s.hooks()["PreToolUse"] if m.matcher == "StructuredOutput")


def test_a_report_the_check_refuses_goes_back_once_in_the_same_session(wt):
    s = RunState(wt, wt, cap=60, wrap_up=0, check_report=lambda out: None if out.get("ok") else "add probes")
    report = _report_hook(s)
    out = asyncio.run(report({"tool_name": "StructuredOutput", "tool_input": {"ok": False}}, "tu1", {}))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "add probes"
    # The second report goes out whatever it holds: one send-back per run, never a loop.
    assert asyncio.run(report({"tool_name": "StructuredOutput", "tool_input": {"ok": False}}, "tu2", {})) == {}
    res = s.see({"type": "result", "permission_denials": [{"tool_name": "StructuredOutput", "tool_use_id": "tu1"}]})
    assert [d.get("agentpipe_hook") for d in res["permission_denials"]] == ["report check"]


def test_a_report_near_the_turn_cap_goes_out_unchecked(wt):
    s = RunState(wt, wt, cap=10, wrap_up=0, check_report=lambda out: "add probes")
    for i in range(7):
        s.see({"type": "assistant", "message": {"id": f"m{i}"}})
    assert asyncio.run(_report_hook(s)({"tool_input": {}}, "tu", {})) == {}


def test_without_a_check_there_is_no_report_hook(wt):
    assert all(m.matcher != "StructuredOutput" for m in RunState(wt, wt, cap=10, wrap_up=0).hooks()["PreToolUse"])


# A stand-in for the Claude Code CLI that speaks the SDK's stream-json control protocol.
FAKE_CLI = r'''
import json, sys, time
mode = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else ""
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") == "control_request":
        out({"type": "control_response", "response": {"subtype": "success",
             "request_id": msg["request_id"], "response": {}}})
    elif msg.get("type") == "user":
        out({"type": "system", "subtype": "init", "session_id": "s1", "cwd": ".", "tools": [],
             "mcp_servers": [], "model": mode, "permissionMode": "dontAsk"})
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
             "unifiedWindows": {"five_hour": {"utilization": 0.5}}}, "uuid": "u", "session_id": "s1"})
        out({"type": "assistant", "session_id": "s1", "parent_tool_use_id": None,
             "message": {"id": "m1", "role": "assistant", "model": mode,
                         "content": [{"type": "text", "text": "hi"}]}})
        if mode == "sleepy":
            time.sleep(60)
        if mode == "crash":
            sys.stderr.write("boom\n"); sys.exit(3)
        out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
             "duration_ms": 1, "duration_api_ms": 1, "session_id": "s1", "result": "done",
             "structured_output": {"ok": True}, "total_cost_usd": 0.01, "permission_denials": []})
        break
'''


@pytest.fixture
def fake_cli(tmp_path):
    """A wrapper the SDK can start as its CLI (an .exe on Windows: it refuses batch files)."""
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    if os.name == "nt":
        from distlib.scripts import ScriptMaker  # an .exe launcher, as pip makes for scripts
        src = tmp_path / "src"
        src.mkdir()
        (src / "claude.py").write_text("#!python\n" + FAKE_CLI, encoding="utf-8")
        maker = ScriptMaker(str(src), str(tmp_path / "bin"))
        maker.executable = sys.executable
        maker.variants = {""}
        made = maker.make("claude.py")
        exe = next(Path(m) for m in made if m.endswith(".exe"))
        return exe
    launcher = tmp_path / "claude"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    launcher.chmod(0o755)
    return launcher


def test_a_session_through_the_sdk_logs_stream_json_and_parses_like_the_cli(fake_cli, tmp_path):
    rates = []
    c = Claude(str(fake_cli), tmp_path / "logs", 30, on_rate=rates.append)
    res = c.run("go", tmp_path, Agent("m", 5, ["Read"]), {"type": "object"}, log_name="impl-x")
    assert res.ok and res.structured == {"ok": True} and res.session_id == "s1" and res.cost_usd == 0.01
    assert rates == [{"five_hour": {"utilization": 0.5}}]
    log = (tmp_path / "logs" / "impl-x.out.json").read_text(encoding="utf-8")
    kinds = [json.loads(l)["type"] for l in log.splitlines() if l.startswith("{")]
    assert kinds == ["system", "rate_limit_event", "assistant", "result"]  # no control traffic


def test_a_crashed_session_reports_its_exit_code(fake_cli, tmp_path):
    c = Claude(str(fake_cli), tmp_path / "logs", 30)
    res = c.run("go", tmp_path, Agent("crash", 5, []), {}, log_name="x")
    assert not res.ok and res.subtype == ""


def test_a_session_past_its_timeout_is_killed_and_reported_as_capped(fake_cli, tmp_path):
    c = Claude(str(fake_cli), tmp_path / "logs", 2)
    res = c.run("go", tmp_path, Agent("sleepy", 5, []), {}, log_name="x")
    assert res.subtype == "timeout" and not res.ok
