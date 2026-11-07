"""Milestone releases against real git repos (a local bare origin) and an in-memory GitHub."""

import subprocess
from pathlib import Path

import pytest

from agentpipe import milestones
from agentpipe.config import Agent, Config, Milestone, Target
from agentpipe.pipeline import RELEASE_KEY, Pipeline
from agentpipe.status import Status

from test_pipeline import FakeGitHub


class ReleasingGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.releases = []

    def create_release(self, tag, title, notes, assets):
        self.releases.append((tag, title, notes, assets))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    write(work, "README.md", "x\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "init")
    git(work, "push", "-q", "origin", "HEAD:main")
    git(work, "checkout", "-qb", "dev")
    write(work, "openspec/changes/a/tasks.md", "- [ ] 1.1 x\n")
    write(work, "openspec/changes/b/tasks.md", "- [ ] 1.1 y\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "specs")
    write(work, "openspec/changes/a/tasks.md", "- [x] 1.1 x\n")
    git(work, "commit", "-qam", "Tick a/1.1 in tasks.md")
    cut = git(work, "rev-parse", "HEAD").strip()
    write(work, "later.txt", "work for the next milestone\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "later work")
    git(work, "push", "-q", "origin", "HEAD:dev")
    return tmp_path, work, origin, cut


def make(tmp_path: Path, work: Path, test: str = "exit 0") -> tuple[Pipeline, ReleasingGitHub]:
    target = Target("t", work, "o/r", "dev", ["a", "b"], ".", test, "", [], tmp_path / "pipe",
                    release_branch="main",
                    milestones=[Milestone("m1", "Milestone 1", ["a"]), Milestone("m2", "Milestone 2", ["b"])])
    agent = Agent("m", 1, [])
    cfg = Config({"t": target}, "claude", "gh", {"planner": agent, "implementer": agent, "reviewer": agent})
    gh = ReleasingGitHub()
    p = Pipeline(cfg, target, gh=gh, log=lambda m: None)
    p.refresh_base()
    return p, gh


def release(p: Pipeline) -> bool:
    p.check_milestones()
    job = p.jobs.pop(RELEASE_KEY, None)
    if job:
        job.future.result()
    return job is not None


def test_next_due_goes_in_order(tmp_path):
    write(tmp_path, "openspec/changes/a/tasks.md", "- [x] 1.1 x\n")
    write(tmp_path, "openspec/changes/b/tasks.md", "- [ ] 1.1 y\n")
    write(tmp_path, "openspec/changes/archive/2026-11-01-c/tasks.md", "- [x] 1.1 z\n")
    ms = [Milestone("m1", "", ["a"]), Milestone("m2", "", ["b"]), Milestone("m3", "", ["c"])]
    assert milestones.next_due(ms, set(), tmp_path).tag == "m1"
    assert milestones.next_due(ms, {"m1"}, tmp_path) is None  # m2 open; m3 waits its turn
    assert milestones.next_due(ms, {"m1", "m2"}, tmp_path).tag == "m3"  # archived counts as done


def test_rank_puts_earlier_milestones_first():
    ms = [Milestone("m1", "", ["b", "c"]), Milestone("m2", "", ["a"])]
    ranks = {c: milestones.rank(ms, c) for c in ("a", "b", "c", "loose", "ticket")}
    assert ranks == {"ticket": -1, "b": 0, "c": 0, "a": 1, "loose": 2}
    keys = ["a/1.1", "loose/1.1", "c/2.1", "b/1.2", "ticket/7"]
    assert sorted(keys, key=lambda k: milestones.rank(ms, k.split("/")[0])) == \
        ["ticket/7", "c/2.1", "b/1.2", "a/1.1", "loose/1.1"]


def test_release_merges_the_cut_commit_into_main_and_tags_it(repo):
    tmp_path, work, origin, cut = repo
    p, gh = make(tmp_path, work)
    assert release(p)
    assert "m1" in p.git.remote_tags()
    git(work, "fetch", "-q", "origin", "--tags")
    main_files = git(work, "ls-tree", "-r", "--name-only", "origin/main")
    assert "openspec/changes/a/tasks.md" in main_files
    assert "later.txt" not in main_files  # work after the milestone stays on dev
    assert git(work, "rev-parse", "m1^{commit}") == git(work, "rev-parse", "origin/main")
    git(work, "merge-base", "--is-ancestor", cut, "origin/main")
    assert [r[0] for r in gh.releases] == ["m1"]
    assert "`a`" in gh.releases[0][2]
    assert not release(p)  # tagged already; m2 isn't done


def test_failed_gate_files_one_issue_and_retries_dev_head_once(repo):
    tmp_path, work, origin, cut = repo
    p, gh = make(tmp_path, work, test="exit 1")
    assert release(p)
    issues = [i for i in gh.issues.values() if "release blocked" in i["title"]]
    assert len(issues) == 1 and Status.NEEDS_DECISION.label in issues[0]["labels"]
    assert release(p)  # dev head differs from the cut commit, so it is tried too
    assert len([i for i in gh.issues.values() if "release blocked" in i["title"]]) == 1
    assert len(gh.comments(issues[0]["number"])) == 1
    assert not release(p)  # nothing new to try until dev moves
    assert "m1" not in p.git.remote_tags() and not gh.releases


def test_milestones_require_a_distinct_release_branch(tmp_path):
    from agentpipe.config import load
    cfg = tmp_path / "p.toml"
    cfg.write_text('[agents.planner]\nmodel="m"\n[agents.implementer]\nmodel="m"\n[agents.reviewer]\nmodel="m"\n'
                   '[targets.x]\nrepo_root="."\ngithub_repo="o/r"\ntest="t"\nbase="dev"\n'
                   '[[targets.x.milestones]]\ntag="m1"\nchanges=["a"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="release_branch"):
        load(cfg)


def test_a_milestone_does_not_wait_for_its_own_play_tasks(tmp_path: Path):
    write(tmp_path, "openspec/changes/a/tasks.md", "- [x] 1.1 x\n- [ ] 1.2 play it\n")
    m = Milestone("step-1", "Step 1", ["a"], play=["a/1.2"])
    assert milestones.next_due([m], set(), tmp_path) is m
    assert milestones.next_due([Milestone("step-1", "Step 1", ["a"])], set(), tmp_path) is None
    assert milestones.play_tags([m]) == {"a/1.2": "step-1"}
