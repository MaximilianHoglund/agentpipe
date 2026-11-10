"""Handoffs only on code changes, open-only and single-issue GitHub reads, the agents' env guard."""

import os
import subprocess
import threading

import pytest

from agentpipe import config, proc
from agentpipe.claude import NO_GH_TOKEN, NO_PUSH_URL, PUSH_PREFIXES, Claude, guard_env
from agentpipe.github import GitHub
from agentpipe.status import Status
from gitutil import git, make_repo
from test_pipeline import FakeGitHub, pl, task_issue  # noqa: F401  (pytest fixture)

S = Status


# ------------------------------------------------------------------ 1. handoff only on code changes
def commit(repo, path, text):
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"edit {path}")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def coded(pl, tmp_path):  # noqa: F811
    """The pipeline, watching a real checkout that has code, a config, a README and a test."""
    p, _ = pl
    (tmp_path / "code").mkdir()
    repo = make_repo(tmp_path / "code")
    for path in ("agentpipe/pipeline.py", "agentpipe/prompts/implementer.md", "pyproject.toml", "uv.lock",
                 "pipeline.toml", "README.md", "tests/test_x.py", "tools/friction.py", "openspec/changes/a/tasks.md"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_bytes(b"one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "files")
    p.CODE_DIR = repo
    p.CODE_CHECK_SECONDS = 0
    p.config_path = p.t.work_dir / "pipeline.toml"
    p.code_rev = p.read_code_rev()
    p.code_imports = lambda: ""
    lines = []
    p._log = lines.append
    return p, repo, lines


@pytest.mark.parametrize("path", ["pipeline.toml", "README.md", "tests/test_x.py", "tools/friction.py",
                                  "openspec/changes/a/tasks.md", "docs/new.md"])
def test_a_commit_that_changes_no_code_moves_the_baseline_without_a_handoff(coded, path):
    p, repo, lines = coded
    new = commit(repo, path, "two\n")
    p.check_code()
    assert p.restart_for is None and p.code_rev == new
    assert len(lines) == 1 and "no code change, no handover" in lines[0] and new[:7] in lines[0]
    p.check_code()  # the same HEAD again: nothing more to say
    assert len(lines) == 1


@pytest.mark.parametrize("path", ["agentpipe/pipeline.py", "agentpipe/prompts/implementer.md",
                                  "agentpipe/new_module.py", "pyproject.toml", "uv.lock"])
def test_a_commit_that_changes_what_the_dispatcher_runs_hands_over(coded, path):
    p, repo, lines = coded
    start = p.code_rev
    new = commit(repo, path, "two\n")
    p.check_code()
    assert p.restart_for == new and p.code_rev == start
    assert any(f"agentpipe {new[:7]} committed: handing over now" in l for l in lines)


def test_a_code_commit_after_skipped_ones_is_compared_with_the_latest_skipped_commit(coded):
    p, repo, lines = coded
    commit(repo, "README.md", "two\n")
    p.check_code()
    docs = commit(repo, "pipeline.toml", "two\n")
    p.check_code()
    assert p.code_rev == docs and p.restart_for is None
    seen = []
    changed = p.code_changed
    p.code_changed = lambda old, new: (seen.append(old), changed(old, new))[1]
    code = commit(repo, "agentpipe/pipeline.py", "two\n")
    p.check_code()
    assert seen == [docs] and p.restart_for == code


def test_code_and_docs_in_one_commit_range_hand_over(coded):
    p, repo, lines = coded
    commit(repo, "agentpipe/pipeline.py", "two\n")
    new = commit(repo, "README.md", "two\n")  # both land between two checks
    p.check_code()
    assert p.restart_for == new


def test_code_changed_and_changed_back_needs_no_handoff(coded):
    p, repo, lines = coded
    commit(repo, "agentpipe/pipeline.py", "two\n")
    new = commit(repo, "agentpipe/pipeline.py", "one\n")
    p.check_code()
    assert p.restart_for is None and p.code_rev == new


def test_a_start_commit_git_no_longer_has_falls_back_to_handing_over(coded):
    p, repo, lines = coded
    p.code_rev = "d" * 40  # rebased away
    new = commit(repo, "README.md", "two\n")
    assert p.code_changed("d" * 40, new)
    p.check_code()
    assert p.restart_for == new


def test_a_checkout_git_cannot_diff_in_falls_back_to_handing_over(coded, tmp_path):
    p, repo, lines = coded
    p.CODE_DIR = tmp_path / "missing"
    assert p.code_changed("a" * 40, "b" * 40)


def test_code_that_does_not_import_is_still_refused_and_docs_on_top_do_not_hide_it(coded):
    p, repo, lines = coded
    start = p.code_rev
    p.code_imports = lambda: "SyntaxError: bad"
    bad = commit(repo, "agentpipe/pipeline.py", "broken\n")
    p.check_code()
    assert p.bad_rev == bad and p.restart_for is None and p.code_rev == start
    docs = commit(repo, "README.md", "two\n")
    p.check_code()  # compared with the commit we run, so the broken code still counts
    assert p.bad_rev == docs and p.restart_for is None and p.code_rev == start
    assert sum("doesn't import" in l for l in lines) == 2 and not any("no code change" in l for l in lines)


# ------------------------------------------------------------------ 2. open-only and single-issue reads
def gh_recording(out="[]"):
    calls = []

    def run(cmd, cwd, input=None):
        calls.append(cmd)
        return proc.Result(0, out, "")

    return GitHub("o/r", run=run), calls


def test_list_issues_asks_for_every_state_unless_told_open():
    gh, calls = gh_recording('[{"number": 1, "state": "OPEN", "title": "t", "body": "b", "labels": [{"name": "pipeline"}]}]')
    assert gh.list_issues() == [{"number": 1, "state": "OPEN", "title": "t", "body": "b", "labels": ["pipeline"]}]
    gh.list_issues(state="open")
    states = [c[c.index("--state") + 1] for c in calls]
    assert states == ["all", "open"]


class CountingGitHub(FakeGitHub):
    """Records which lists and single issues were read."""

    def __init__(self):
        super().__init__()
        self.listed, self.viewed = [], []

    def list_issues(self, state="all"):
        self.listed.append(state)
        return super().list_issues(state)

    def issue(self, number):
        self.viewed.append(number)
        return super().issue(number)


@pytest.fixture
def counted(pl):  # noqa: F811
    p, _ = pl
    p.gh = gh = CountingGitHub()
    return p, gh


def test_scheduling_lists_open_issues_only_and_ignores_closed_ones(counted):
    p, gh = counted
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    closed = gh.add("<!-- pipeline-task: c/1.2 -->", S.PENDING_IMPLEMENTATION, state="CLOSED")
    gh.add("no marker", S.NEEDS_DECISION, state="CLOSED")
    waiting = gh.add("no marker", S.NEEDS_DECISION)
    p.tick()
    assert gh.listed == ["open"]
    assert p.started == [("implement", "c/1.1", "impl-0")]
    assert closed not in {i["number"] for i in p.issue_map.values()}
    assert p.state.data["awaiting_input"] == [waiting]


def test_the_waiting_count_thread_lists_open_issues_only(counted):
    p, gh = counted
    p.cfg.poll_seconds = 0
    gh.add("no marker", S.NEEDS_DECISION, state="CLOSED")
    waiting = gh.add("no marker", S.PROPOSED)
    done = threading.Event()
    listing = gh.list_issues
    gh.list_issues = lambda state="all": (done.set(), listing(state))[1]
    p.refresh_awaiting_forever(done)
    assert gh.listed == ["open"] and p.state.data["awaiting_input"] == [waiting]


def test_one_issues_labels_and_state_are_read_without_listing(counted):
    p, gh = counted
    n = task_issue(gh, "c/1.1", S.CHANGES_REQUESTED)
    shut = gh.add("x", S.FINISHED, state="CLOSED")
    assert p.gh_issue_labels(n) == ["pipeline", S.CHANGES_REQUESTED.label]
    assert p.gh_issue_open(n) and not p.gh_issue_open(shut)
    assert gh.listed == [] and gh.viewed == [n, n, shut]


def test_a_job_that_failed_twice_finds_its_issue_in_the_open_list(counted):
    p, gh = counted
    gh.add("<!-- pipeline-task: c/1.1 -->", S.FINISHED, state="CLOSED")  # an earlier, closed issue
    n = task_issue(gh, "c/1.1", S.IN_PROGRESS)
    p.bump_failure("c/1.1", "implement", "boom")
    assert gh.listed == []  # the first failure only counts
    p.bump_failure("c/1.1", "implement", "boom")
    assert gh.listed == ["open"]
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]
    assert "failed twice" in gh.comments_by[n][-1]["body"]
    assert p.state.task("c/1.1")["fail_implement"] == 0


# ------------------------------------------------------------------ 4. the agents' env guard
def test_guard_env_logs_gh_out_and_rewrites_pushes_without_touching_its_input(tmp_path):
    given = {"PATH": "x", "GH_TOKEN": "real", "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false"}
    before = dict(given)
    env = guard_env(given, tmp_path / "no-gh")
    assert given == before
    assert env["PATH"] == "x" and env["GH_TOKEN"] == NO_GH_TOKEN
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "no-gh") and list((tmp_path / "no-gh").iterdir()) == []
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("core.autocrlf", "false")  # kept
    n = len(PUSH_PREFIXES)
    assert env["GIT_CONFIG_COUNT"] == str(1 + n)
    assert {env[f"GIT_CONFIG_KEY_{i}"] for i in range(1, 1 + n)} == {f"url.{NO_PUSH_URL}.pushInsteadOf"}
    assert [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(1, 1 + n)] == list(PUSH_PREFIXES)


def test_guard_env_starts_from_this_process_when_given_none(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPIPE_TEST_MARK", "here")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = guard_env(None, tmp_path / "no-gh")
    assert env["AGENTPIPE_TEST_MARK"] == "here" and env["GIT_CONFIG_COUNT"] == str(len(PUSH_PREFIXES))
    assert "GIT_CONFIG_COUNT" not in os.environ


def run_git(repo, env, *args):
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=60)


def test_with_the_guard_git_pushes_to_a_network_url_fail_and_local_work_goes_on(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    repo = make_repo(tmp_path)
    git(repo, "remote", "add", "origin", "https://example.invalid/o/r.git")
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    env = guard_env(dict(os.environ, GIT_TERMINAL_PROMPT="0"), tmp_path / "no-gh")
    # However the push is phrased, it goes to the unusable URL, not to the network.
    for args in (["push", "--dry-run", "origin", "HEAD:refs/heads/x"],
                 ["-C", ".", "push", "--dry-run", "origin", "HEAD:refs/heads/x"],
                 ["-c", "a.b=c", "push", "--dry-run", "origin", "HEAD:refs/heads/x"],
                 ["push", "--dry-run", "https://example.invalid/o/r.git", "HEAD:refs/heads/x"],
                 ["push", "--dry-run", "git@example.invalid:o/r.git", "HEAD:refs/heads/x"],
                 ["push", "--dry-run", "ssh://git@example.invalid/o/r.git", "HEAD:refs/heads/x"]):
        r = run_git(repo, env, *args)
        assert r.returncode != 0 and "no-push" in r.stderr, (args, r.stderr)
    assert run_git(repo, env, "remote", "get-url", "--push", "origin").stdout.startswith(NO_PUSH_URL)
    # Fetching still uses the real URL, and local work is untouched.
    assert run_git(repo, env, "remote", "get-url", "origin").stdout.strip() == "https://example.invalid/o/r.git"
    (repo / "new.rs").write_bytes(b"y\n")
    assert run_git(repo, env, "add", "-A").returncode == 0
    assert run_git(repo, env, "commit", "-qm", "local").returncode == 0
    assert run_git(repo, env, "status", "--porcelain").stdout == ""
    assert run_git(repo, env, "push", "-q", str(bare), "HEAD:refs/heads/x").returncode == 0  # a test's scratch remote
    assert run_git(repo, env, "fetch", "-q", str(bare), "x").returncode == 0


def claude_envs(p):
    """Run the pipeline's Claude with a recorded process; returns the envs it was started with."""
    envs = []

    def fake_run(cmd, cwd, **kw):
        envs.append(kw.get("env"))
        return proc.Result(0, '{"type": "result", "subtype": "success", "result": "", "structured_output": {}}', "")

    p.claude._run = fake_run
    return envs


def test_every_agent_run_gets_the_guard_and_gates_do_not(pl, monkeypatch):  # noqa: F811
    p, _ = pl
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setenv("GH_TOKEN", "the-dispatchers-own")
    envs = claude_envs(p)
    agent = p.cfg.agents["implementer"]
    p.claude.run("hi", p.t.work_dir, agent, {}, log_name="impl", env=p.env("impl-0"))  # implementer, reviewer, preflight
    p.claude.run("hi", p.t.work_dir, agent, {}, log_name="plan")  # planner and triage pass no env
    for env in envs:
        assert env["GH_TOKEN"] == NO_GH_TOKEN and env["GH_CONFIG_DIR"] == str(p.t.work_dir / "no-gh")
        assert env["GIT_CONFIG_COUNT"] == str(len(PUSH_PREFIXES))
    assert envs[0]["CARGO_TARGET_DIR"] == str(p.t.work_dir / "target" / "impl-0")  # the slot's env is kept
    assert "CARGO_TARGET_DIR" not in envs[1] and envs[1]["PATH"] == os.environ["PATH"]
    gate = p.env("impl-0")  # what gates, mutation runs and format commands get
    assert gate["GH_TOKEN"] == "the-dispatchers-own" and "GIT_CONFIG_COUNT" not in gate and "GH_CONFIG_DIR" not in gate
    assert os.environ["GH_TOKEN"] == "the-dispatchers-own"  # the dispatcher's own gh and git calls


def test_env_guard_off_leaves_agent_runs_their_plain_env(pl, monkeypatch):  # noqa: F811
    p, _ = pl
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    envs = claude_envs(p)
    p.cfg.env_guard = False  # as a config reload sets it: read at each run
    agent = p.cfg.agents["implementer"]
    p.claude.run("hi", p.t.work_dir, agent, {}, log_name="impl", env=p.env("impl-0"))
    p.claude.run("hi", p.t.work_dir, agent, {}, log_name="plan")
    assert "GIT_CONFIG_COUNT" not in envs[0] and "GH_CONFIG_DIR" not in envs[0] and envs[1] is None


def test_a_claude_without_a_guard_passes_env_through(tmp_path):
    seen = []

    def fake_run(cmd, cwd, **kw):
        seen.append(kw.get("env"))
        return proc.Result(0, "{}", "")

    from agentpipe.config import Agent
    Claude("claude", tmp_path / "logs", 60, run=fake_run).run("hi", tmp_path, Agent("m", 1, []), {},
                                                              log_name="x", env={"A": "1"})
    assert seen == [{"A": "1"}]


TOML = """
[permissions]
deny = []
{extra}
[agents.planner]
model = "m"
[agents.implementer]
model = "m"
[agents.reviewer]
model = "m"
"""


def test_env_guard_is_on_unless_permissions_turns_it_off(tmp_path):
    f = tmp_path / "pipeline.toml"
    f.write_text(TOML.format(extra=""), encoding="utf-8")
    assert config.load(f).env_guard is True
    f.write_text(TOML.format(extra="env_guard = false"), encoding="utf-8")
    assert config.load(f).env_guard is False
