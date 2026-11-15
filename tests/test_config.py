from pathlib import Path

from agentpipe import config, proc
from agentpipe.claude import Claude

TOML = """
[permissions]
deny = ["Bash(git push:*)"]
[agents.planner]
model = "m"
tools = ["Read", "Bash"]
[agents.implementer]
model = "m"
tools = ["Bash"]
deny = ["Bash(cargo publish:*)"]
[agents.reviewer]
model = "m"
"""


def test_shared_deny_reaches_every_agent_and_the_claude_command(tmp_path):
    p = tmp_path / "pipeline.toml"
    p.write_text(TOML, encoding="utf-8")
    c = config.load(p)
    assert c.agents["planner"].deny == ["Bash(git push:*)"]
    assert c.agents["implementer"].deny == ["Bash(git push:*)", "Bash(cargo publish:*)"]
    seen = {}

    def fake_run(options, **kw):
        seen["options"] = options
        return proc.Result(0, '{"subtype": "success", "result": "", "structured_output": {}}', "")

    Claude("claude", tmp_path / "logs", 60, run=fake_run).run(
        "hi", tmp_path, c.agents["implementer"], {}, log_name="x")
    o = seen["options"]
    assert o.disallowed_tools == ["Bash(git push:*)", "Bash(cargo publish:*)"]
    assert o.allowed_tools == c.agents["implementer"].tools
    assert o.permission_mode == "dontAsk" and o.max_turns == c.agents["implementer"].max_turns
    # The SDK's defaults would drop Claude Code's system prompt and the target's settings files.
    assert o.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert o.setting_sources == ["user", "project", "local"]
    assert o.hooks and set(o.hooks) == {"PreToolUse", "PostToolUse"}


def test_wrap_up_defaults_to_fifteen_percent_of_the_cap(tmp_path):
    p = tmp_path / "pipeline.toml"
    p.write_text(TOML.replace("[agents.implementer]", "[agents.implementer]\nwrap_up = 0"), encoding="utf-8")
    c = config.load(p)
    assert c.agents["implementer"].wrap_turns() == 0
    assert config.Agent("m", 100, []).wrap_turns() == 15
    assert config.Agent("m", 60, []).wrap_turns() == 9
    assert config.Agent("m", 10, []).wrap_turns() == 3


def test_shipped_config_denies_pushing_and_gh_for_all_agents():
    c = config.load(Path(__file__).resolve().parent.parent / "pipeline.toml")
    for a in c.agents.values():
        assert {"Bash(git push:*)", "Bash(gh:*)"} <= set(a.deny)
        assert not any(r.startswith("PowerShell(Remove-Item:") or r.startswith("Bash(rm ") for r in a.deny)


MACHINES = TOML + """
work_root = "/w"
[targets.a]
repo_root = "/a"
github_repo = "o/a"
test = "t"
[targets.b]
repo_root = "/b"
github_repo = "o/b"
test = "t"
[machines.one]
hostnames = ["HOST-ONE"]
targets = ["a"]
[machines.two]
targets = ["b"]
work_root = "/w2"
gh_bin = "/bin/gh"
[machines.two.target.b]
repo_root = "/elsewhere/b"
test = "bash check.sh"
"""


def test_a_machine_runs_only_its_targets_with_its_overrides(tmp_path, monkeypatch):
    p = tmp_path / "pipeline.toml"
    p.write_text(MACHINES, encoding="utf-8")
    monkeypatch.setenv("AGENTPIPE_MACHINE", "two")
    c = config.load(p)
    assert c.machine == "two"
    assert c.runs_here("b") and not c.runs_here("a")
    assert c.gh_bin == "/bin/gh"
    b = c.targets["b"]
    assert (b.repo_root, b.test, b.work_dir) == (Path("/elsewhere/b"), "bash check.sh", Path("/w2/b"))
    assert c.targets["a"].repo_root == Path("/a")


def test_the_machine_is_found_by_hostname_and_none_matching_runs_everything(tmp_path, monkeypatch):
    p = tmp_path / "pipeline.toml"
    p.write_text(MACHINES, encoding="utf-8")
    monkeypatch.delenv("AGENTPIPE_MACHINE", raising=False)
    monkeypatch.setattr(config.socket, "gethostname", lambda: "host-one")
    c = config.load(p)
    assert c.machine == "one" and c.runs_here("a") and not c.runs_here("b")
    monkeypatch.setattr(config.socket, "gethostname", lambda: "other")
    c = config.load(p)
    assert c.machine == "" and c.runs_here("a") and c.runs_here("b")


def test_an_unknown_machine_name_is_an_error(tmp_path, monkeypatch):
    p = tmp_path / "pipeline.toml"
    p.write_text(MACHINES, encoding="utf-8")
    monkeypatch.setenv("AGENTPIPE_MACHINE", "three")
    try:
        config.load(p)
    except ValueError as e:
        assert "three" in str(e)
    else:
        raise AssertionError("expected a ValueError")
