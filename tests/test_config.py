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

    def fake_run(cmd, cwd, **kw):
        seen["cmd"] = cmd
        return proc.Result(0, '{"subtype": "success", "result": "", "structured_output": {}}', "")

    Claude("claude", tmp_path / "logs", 60, run=fake_run).run(
        "hi", tmp_path, c.agents["implementer"], {}, log_name="x")
    cmd = seen["cmd"]
    i = cmd.index("--disallowedTools")
    assert cmd[i + 1:i + 3] == ["Bash(git push:*)", "Bash(cargo publish:*)"]
    assert cmd[i + 3] == "--allowedTools"


def test_shipped_config_denies_pushing_and_gh_for_all_agents():
    c = config.load(Path(__file__).resolve().parent.parent / "pipeline.toml")
    for a in c.agents.values():
        assert {"Bash(git push:*)", "Bash(gh:*)", "PowerShell(Remove-Item:*)"} <= set(a.deny)
