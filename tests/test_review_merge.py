"""The merge path's failure handling: retried merges and gates, merges that were never finished,
closed issues with unticked tasks, and failure counters."""

import json
from pathlib import Path

import pytest

from agentpipe import pipeline as pipeline_mod
from agentpipe import proc, review
from agentpipe.github import GitHub
from agentpipe.status import TASK_MARKER, Status
from gitutil import git
from test_pipeline import _Job, archive_repo, pl, task_issue, train_setup  # noqa: F401  (pytest fixture)

S = Status
BASE_MOVED = "GraphQL: Base branch was modified. Review and try the merge again. (mergePullRequest)"


def gh_error(text):
    return proc.CommandError(["gh", "pr", "merge"], proc.Result(1, "", text))


def failing_merges(gh, errors):
    """``gh.merge_pr`` raises each of ``errors`` in turn, then merges; returns the PRs it was asked for."""
    errors, asked = list(errors), []

    def merge_pr(number, sha):
        asked.append(number)
        if errors:
            raise gh_error(errors.pop(0))
    gh.merge_pr = merge_pr
    return asked


def logging(p):
    lines = []
    p._log = lines.append
    return lines


class StubGit:
    """Git for merge_one and job_review: a branch at `head1`, up to date with the base or not."""

    def __init__(self, contains=True):
        self.has_base, self.fetches, self.pushes = contains, 0, 0

    def fetch(self): self.fetches += 1
    def git(self, *a, **kw): return proc.Result(0, "", "")
    def head(self, wt): return "head1"
    def contains(self, wt, ref): return self.has_base
    def rev(self, wt, ref): return "base1"
    def push(self, wt, branch): self.pushes += 1
    def worktree_detached(self, path, ref): pass
    def worktree_remove(self, path): pass


def approved(p, gh, monkeypatch, contains=True):
    """An approved c/1.1 with PR #7, stubbed git, and no waiting between merge attempts."""
    monkeypatch.setattr(pipeline_mod, "MERGE_RETRY_SECONDS", 0)
    n = task_issue(gh, "c/1.1", S.APPROVED)
    p.git = StubGit(contains)
    p.ensure_worktree = lambda t: Path(".")
    monkeypatch.setattr(p, "merge_base", lambda wt, slot, gate: "clean")
    finished = []
    monkeypatch.setattr(p, "finish_merge", lambda t, issue, pr, **kw: finished.append((t.key, kw)))
    return p.tasks["c/1.1"], n, {"number": 7, "headRefOid": "head1"}, finished


def gates(p, monkeypatch, results):
    """The gate passes or fails as ``results`` says, in order; returns the list, emptied as it runs."""
    results = list(results)
    monkeypatch.setattr(p, "run_gate", lambda *a, **kw: proc.Result(0 if results.pop(0) else 1, "boom", ""))
    return results


# ------------------------------------------------------------------ 1. "Base branch was modified"
def test_a_base_that_moved_during_a_squash_merge_is_retried(pl, monkeypatch):
    p, gh = pl
    t, n, pr, _ = approved(p, gh, monkeypatch)
    asked = failing_merges(gh, [BASE_MOVED, "HTTP 405: Pull Request is not mergeable"])
    assert p.squash_merge(t, pr) is True and asked == [7, 7, 7]
    asked = failing_merges(gh, [BASE_MOVED] * 9)
    assert p.squash_merge(t, pr) is False and len(asked) == pipeline_mod.MERGE_ATTEMPTS


def test_other_merge_errors_still_raise(pl, monkeypatch):
    p, gh = pl
    t, n, pr, _ = approved(p, gh, monkeypatch)
    failing_merges(gh, ["HTTP 403: Resource not accessible"])
    with pytest.raises(proc.CommandError):
        p.squash_merge(t, pr)
    failing_merges(gh, ["HTTP 403: Resource not accessible"])
    with pytest.raises(proc.CommandError):
        p.merge_one(t, n, pr, "merge")


def test_merging_one_pr_fetches_and_tries_again_when_the_base_moved(pl, monkeypatch):
    p, gh = pl
    t, n, pr, finished = approved(p, gh, monkeypatch)
    asked = failing_merges(gh, [BASE_MOVED])
    p.merge_one(t, n, pr, "merge")
    assert asked == [7, 7] and [k for k, _ in finished] == ["c/1.1"]
    assert p.git.fetches == 2  # once at the start, once before the second attempt


def test_a_base_that_keeps_moving_leaves_the_pr_approved_not_sent_back_as_a_conflict(pl, monkeypatch):
    p, gh = pl
    t, n, pr, finished = approved(p, gh, monkeypatch)
    asked = failing_merges(gh, [BASE_MOVED] * 9)
    p.merge_one(t, n, pr, "merge")
    assert len(asked) == pipeline_mod.MERGE_ATTEMPTS and finished == []
    assert S.APPROVED.label in gh.issues[n]["labels"] and gh.comments(7) == []
    # GitHub still calling it unmergeable is a conflict, as before.
    failing_merges(gh, ["not mergeable"] * 9)
    p.merge_one(t, n, pr, "merge")
    assert S.CHANGES_REQUESTED.label in gh.issues[n]["labels"] and len(gh.comments(7)) == 1


# ------------------------------------------------------------------ 2. a batch merge that raises midway
THREE = {"c/1.1": ("a.rs", "a\n"), "c/1.2": ("b.rs", "b\n"), "c/1.3": ("c.rs", "c\n")}


def test_a_batch_merge_that_raises_midway_still_finishes_the_prs_already_merged(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch, THREE)

    def merge_pr(number, sha):
        if number == 21:
            raise gh_error("HTTP 500: something went wrong")
        seen["merged"].append(number)
    gh.merge_pr = merge_pr
    with pytest.raises(proc.CommandError, match="HTTP 500"):
        p.merge_train(order, "merge")
    assert seen["merged"] == [20] and seen["finished"] == ["c/1.1"]  # #22 waits for the next merge
    assert p.merging == set()


def test_a_failing_fetch_after_a_batch_merged_still_finishes_it(pl, tmp_path, monkeypatch):
    p, gh = pl
    order, seen = train_setup(p, gh, tmp_path, monkeypatch, THREE)
    fetches = []

    def fetch():
        fetches.append(1)
        if len(fetches) > 1:  # the one at the start works
            raise gh_error("cannot lock ref 'refs/remotes/origin/dev'")
    p.git.fetch = fetch
    assert p.merge_train(order, "merge") == "merged"
    assert seen["finished"] == ["c/1.1", "c/1.2", "c/1.3"]


# ------------------------------------------------------------------ 3a. merged, never finished
def merged_pr(key, number=30, head="mergedsha"):
    change, tid = key.split("/")
    return {"number": number, "headRefName": f"pipeline/{change}/{tid}", "headRefOid": head,
            "body": TASK_MARKER.format(key=key), "title": "t", "mergedAt": "2026-11-10T10:00:00Z"}


@pytest.mark.parametrize("status", [S.APPROVED, S.PENDING_REVIEW, S.CHANGES_REQUESTED])
def test_an_issue_whose_pr_was_merged_is_finished_instead_of_flagged(pl, status):
    p, gh = pl
    n = task_issue(gh, "c/1.1", status)
    gh.merged = [merged_pr("c/1.1")]
    started = []
    record = p.start
    p.start = lambda kind, key, slot, issue, pr: (started.append((kind, key, slot, issue["number"], pr)),
                                                  record(kind, key, slot, issue, pr))
    p.tick()
    assert started == [("merge", "c/1.1", "merge", n, gh.merged[0])]
    assert status.label in gh.issues[n]["labels"] and gh.comments(n) == []
    p.tick()  # the job holds the key: nothing more happens
    assert len(started) == 1 and status.label in gh.issues[n]["labels"]


def test_finishing_a_merged_pr_takes_the_merge_slot(pl):
    p, gh = pl
    lost = task_issue(gh, "c/1.1", S.APPROVED)
    task_issue(gh, "c/1.2", S.APPROVED)
    gh.prs = [{"number": 12, "headRefOid": "sha2", "body": TASK_MARKER.format(key="c/1.2")}]
    gh.merged = [merged_pr("c/1.1")]
    p.tick()
    assert p.started == [("merge", "c/1.1", "merge")]  # c/1.2's merge waits its turn
    assert S.APPROVED.label in gh.issues[lost]["labels"]


def test_an_issue_without_an_open_pr_waits_while_a_merge_runs_then_is_looked_up(pl):
    p, gh = pl
    n = task_issue(gh, "c/1.1", S.APPROVED)
    p.jobs["c/1.2"] = _Job("merge", "merge")
    looked = []
    gh.merged_pr = lambda branch: looked.append(branch)
    p.tick()
    assert looked == [] and p.started == [] and S.APPROVED.label in gh.issues[n]["labels"]
    del p.jobs["c/1.2"]
    p.tick()  # nothing was merged from its branch: now it is Max's
    assert looked == ["pipeline/c/1.1"] and S.NEEDS_DECISION.label in gh.issues[n]["labels"]


def test_a_merged_pr_of_another_task_or_an_older_head_does_not_count(pl):
    p, gh = pl
    n = task_issue(gh, "c/1.1", S.APPROVED)
    gh.merged = [dict(merged_pr("c/1.1"), body=TASK_MARKER.format(key="c/1.2"))]
    assert p.merged_unfinished("c/1.1") is None
    gh.merged = [merged_pr("c/1.1")]
    assert p.merged_unfinished("c/1.1") == gh.merged[0]  # its branch is gone: nothing newer
    # The task was reopened and its branch pushed again since that PR merged.
    p.git.git = lambda *a, **kw: proc.Result(0, "newersha\n", "")
    assert p.merged_unfinished("c/1.1") is None
    p.git.git = lambda *a, **kw: proc.Result(0, "mergedsha\n", "")  # the branch was never deleted
    assert p.merged_unfinished("c/1.1") == gh.merged[0]
    p.git.git = lambda *a, **kw: proc.Result(0, "newersha\n", "")
    p.tick()
    assert p.started == [] and S.NEEDS_DECISION.label in gh.issues[n]["labels"]


def test_the_merge_job_of_a_merged_pr_only_finishes_it(pl, monkeypatch):
    p, gh = pl
    t, n, _, finished = approved(p, gh, monkeypatch)
    asked = failing_merges(gh, [])
    monkeypatch.setattr(p, "merge_one", lambda *a: pytest.fail("merged already"))
    p.merge_order = [(t, n, merged_pr("c/1.1")), (p.tasks["c/1.2"], 99, {"number": 8, "headRefOid": "x"})]
    p.job_merge(t, n, merged_pr("c/1.1"), "merge")
    assert finished == [("c/1.1", {"cur": None})] and asked == []


def finishing(p, gh, tmp_path, tasks_md):
    """A real origin and base worktree for finish_merge; the fake GitHub closes issues."""
    origin = archive_repo(p, tmp_path, tasks_md)
    p.t.archive = ""
    gh.close_issue = lambda n, reason="completed": gh.issues[n].update(state="CLOSED", stateReason="COMPLETED")
    gh.blocking = lambda n: []
    p.tasks = {t.key: t for t in pipeline_mod.parse_tasks("c", tasks_md)}
    return origin


def tasks_md_on(origin):
    return git(origin, "show", "main:openspec/changes/c/tasks.md")


def test_finish_merge_can_run_again_after_any_part_of_it_already_happened(pl, tmp_path):
    p, gh = pl
    origin = finishing(p, gh, tmp_path, "- [ ] 1.1 a\n- [ ] 1.2 b\n")
    n = task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    follower = task_issue(gh, "c/1.2", S.BLOCKED_BY_TASK)
    p.state.task("c/1.1").update(batch=["c/1.1", "c/1.2"], batch_issues={"c/1.2": follower}, fail_merge=1)
    t, pr = p.tasks["c/1.1"], merged_pr("c/1.1")
    p.finish_merge(t, n, pr, cur=None)
    assert tasks_md_on(origin) == "- [x] 1.1 a\n- [x] 1.2 b\n"
    assert [gh.issues[i]["state"] for i in (n, follower)] == ["CLOSED", "CLOSED"]
    assert S.FINISHED.label in gh.issues[n]["labels"] and "c/1.1" not in p.state.data["tasks"]
    commits = git(origin, "rev-list", "--count", "main").strip()
    # Again: issue closed, tasks ticked, no worktree, no branches, no state entry.
    p.finish_merge(t, n, pr, cur=None)
    p.finish_merge(t, n, pr)
    assert git(origin, "rev-list", "--count", "main").strip() == commits
    assert tasks_md_on(origin) == "- [x] 1.1 a\n- [x] 1.2 b\n"


def test_finish_merge_after_the_change_was_archived_does_not_fail(pl, tmp_path):
    p, gh = pl
    finishing(p, gh, tmp_path, "- [x] 1.1 a\n")
    n = task_issue(gh, "c/1.1", S.APPROVED)
    t = p.tasks["c/1.1"]
    p.refresh_base()
    (p.base_wt / "openspec" / "changes" / "c" / "tasks.md").unlink()
    git(p.base_wt, "commit", "-qam", "archived")
    git(p.base_wt, "push", "-q", "origin", "HEAD:main")
    assert p.commit_to_base([t], "Tick c/1.1") is False
    p.finish_merge(t, n, merged_pr("c/1.1"))
    assert gh.issues[n]["state"] == "CLOSED"


# ------------------------------------------------------------------ 3b. closed as completed, unticked
def closed(gh, key, status, reason="COMPLETED"):
    n = task_issue(gh, key, status)
    gh.issues[n].update(state="CLOSED", stateReason=reason)
    return n


def test_a_task_whose_issue_was_closed_as_completed_is_ticked(pl, tmp_path):
    p, gh = pl
    origin = finishing(p, gh, tmp_path, "- [ ] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n- [ ] 1.5 e\n")
    lines = logging(p)
    done = closed(gh, "c/1.1", S.FINISHED)
    labelled = closed(gh, "c/1.2", S.APPROVED)  # its PR's "Closes #n" closed it; no merged PR is found
    dropped = closed(gh, "c/1.3", S.PENDING_IMPLEMENTATION, "NOT_PLANNED")
    closed(gh, "c/1.4", S.APPROVED)  # a merge job is finishing it right now
    unknown = closed(gh, "c/1.5", S.FINISHED, None)  # no close reason: left alone
    p.jobs["c/1.4"] = _Job("review", "review-0")
    p.refresh_base()
    tasks = p.load_tasks()
    assert p.tick_closed(gh, tasks, p.issues_by_key(gh.list_issues())) is True
    assert tasks_md_on(origin) == "- [x] 1.1 a\n- [x] 1.2 b\n- [ ] 1.3 c\n- [ ] 1.4 d\n- [ ] 1.5 e\n"
    assert S.FINISHED.label in gh.issues[labelled]["labels"]
    assert S.PENDING_IMPLEMENTATION.label in gh.issues[dropped]["labels"]
    said = [l for l in lines if "ticked c/1.1, c/1.2 in tasks.md" in l]
    assert len(said) == 1 and f"#{done}" in said[0] and f"#{labelled}" in said[0] and f"#{unknown}" not in said[0]
    # Nothing left to do: no second commit, no second log line.
    commits, logged = git(origin, "rev-list", "--count", "main").strip(), len(lines)
    assert p.tick_closed(gh, p.load_tasks(), p.issues_by_key(gh.list_issues())) is False
    assert git(origin, "rev-list", "--count", "main").strip() == commits and len(lines) == logged


def test_a_batch_follower_of_a_running_job_is_not_ticked_early(pl, tmp_path):
    p, gh = pl
    origin = finishing(p, gh, tmp_path, "- [ ] 1.1 a\n- [ ] 1.2 b\n")
    task_issue(gh, "c/1.1", S.APPROVED)
    closed(gh, "c/1.2", S.FINISHED)  # the leader's merge job closed it and is about to tick both
    p.state.task("c/1.1")["batch"] = ["c/1.1", "c/1.2"]
    p.jobs["c/1.1"] = _Job("merge", "merge")
    p.refresh_base()
    assert p.tick_closed(gh, p.load_tasks(), p.issues_by_key(gh.list_issues())) is False
    assert tasks_md_on(origin) == "- [ ] 1.1 a\n- [ ] 1.2 b\n"


def test_a_closed_issue_whose_pr_was_merged_gets_the_whole_follow_up(pl, tmp_path, monkeypatch):
    p, gh = pl
    finishing(p, gh, tmp_path, "- [ ] 1.1 a\n- [ ] 1.2 b\n")
    n = closed(gh, "c/1.1", S.APPROVED)
    gh.merged = [merged_pr("c/1.1")]
    finished = []
    monkeypatch.setattr(p, "finish_merge", lambda t, issue, pr, **kw: finished.append((t.key, issue, pr["number"], kw)))
    p.refresh_base()
    tasks = p.load_tasks()
    p.jobs["c/1.2"] = _Job("merge", "merge")  # the base worktree is another merge's for now
    assert p.tick_closed(gh, tasks, p.issues_by_key(gh.list_issues())) is False and finished == []
    del p.jobs["c/1.2"]
    assert p.tick_closed(gh, tasks, p.issues_by_key(gh.list_issues())) is True
    assert finished == [("c/1.1", n, 30, {"cur": None})]


def test_a_failing_tick_is_logged_not_raised_into_the_sync(pl, tmp_path, monkeypatch):
    p, gh = pl
    finishing(p, gh, tmp_path, "- [ ] 1.1 a\n")
    closed(gh, "c/1.1", S.FINISHED)
    lines = logging(p)
    p.refresh_base()
    tasks = p.load_tasks()
    monkeypatch.setattr(p, "commit_to_base", lambda *a: (_ for _ in ()).throw(RuntimeError("index.lock exists")))
    assert p.tick_closed(gh, tasks, p.issues_by_key(gh.list_issues())) is False
    assert any("could not tick c/1.1" in l and "index.lock" in l for l in lines)


def test_a_dry_run_sync_only_says_what_it_would_tick(pl, tmp_path):
    from agentpipe.github import DryRunGitHub
    p, gh = pl
    origin = finishing(p, gh, tmp_path, "- [ ] 1.1 a\n")
    closed(gh, "c/1.1", S.APPROVED)
    lines = logging(p)
    p.refresh_base()
    issues = p.issues_by_key(gh.list_issues())
    assert p.tick_closed(DryRunGitHub("o/r", log=lambda m: None), p.load_tasks(), issues) is False
    assert tasks_md_on(origin) == "- [ ] 1.1 a\n" and any("[dry-run] tick c/1.1" in l for l in lines)


def recording_gh(out):
    calls = []

    def run(cmd, cwd, input=None, **kw):
        calls.append(cmd)
        return proc.Result(0, out, "")
    return GitHub("o/r", run=run), calls


def test_list_issues_asks_for_the_close_reason():
    gh, calls = recording_gh(json.dumps([{"number": 1, "state": "CLOSED", "stateReason": "NOT_PLANNED",
                                          "title": "t", "body": "b", "labels": [{"name": "pipeline"}]}]))
    assert gh.list_issues()[0]["stateReason"] == "NOT_PLANNED"
    assert "stateReason" in calls[0][calls[0].index("--json") + 1].split(",")


def test_merged_pr_is_the_latest_merged_pr_from_that_branch():
    older = dict(merged_pr("c/1.1", 5), mergedAt="2026-11-01T10:00:00Z")
    other = dict(merged_pr("c/1.1", 9), headRefName="pipeline/c/1.10", mergedAt="2026-11-11T10:00:00Z")
    gh, calls = recording_gh(json.dumps([older, merged_pr("c/1.1", 6), other]))
    assert gh.merged_pr("pipeline/c/1.1")["number"] == 6
    cmd = calls[0]
    assert cmd[1:3] == ["pr", "list"] and cmd[cmd.index("--state") + 1] == "merged"
    assert cmd[cmd.index("--head") + 1] == "pipeline/c/1.1" and "--slurp" not in cmd
    gh, _ = recording_gh("[]")
    assert gh.merged_pr("pipeline/c/1.1") is None


# ------------------------------------------------------------------ 4. one gate retry everywhere
def test_the_shared_gate_helper_runs_a_failed_gate_once_more(pl, monkeypatch):
    p, gh = pl
    lines = logging(p)
    left = gates(p, monkeypatch, [True, False, True, False, False, True])
    assert p.gate_retrying(Path("."), "s", "merge c/1.1").ok and len(left) == 5 and lines == []
    assert p.gate_retrying(Path("."), "s", "merge c/1.1").ok and len(left) == 3
    assert "merge c/1.1: the gate failed once, then passed (flaky)" in lines[-1]
    assert not p.gate_retrying(Path("."), "s", "merge c/1.1").ok and len(left) == 1
    assert not any("flaky" in l for l in lines[2:])


def reviewing(p, gh, monkeypatch, gate_results):
    """job_review on c/1.1's PR #7 with stubbed git and a reviewer that approves."""
    n = task_issue(gh, "c/1.1", S.PENDING_REVIEW)
    p.git = StubGit()
    monkeypatch.setattr(p, "merge_base_for_review", lambda rv: None)
    reviews = []
    monkeypatch.setattr(p, "reviewer_verdict", lambda *a, **kw: (reviews.append(1), {
        "verdict": review.APPROVE, "summary": "fine", "findings": [], "probes": []})[1])
    left = gates(p, monkeypatch, gate_results)
    pr = {"number": 7, "headRefOid": "head1", "body": TASK_MARKER.format(key="c/1.1")}
    p.job_review(p.tasks["c/1.1"], n, pr, "review-0")
    return n, left, reviews


def test_a_review_gate_that_fails_once_then_passes_costs_no_round(pl, monkeypatch):
    p, gh = pl
    n, left, reviews = reviewing(p, gh, monkeypatch, [False, True])
    assert left == [] and reviews == [1] and S.APPROVED.label in gh.issues[n]["labels"]
    assert review.rounds_used(review.review_history(gh.comments(7))) == 0


def test_a_review_gate_that_fails_twice_is_a_rejection(pl, monkeypatch):
    p, gh = pl
    n, left, reviews = reviewing(p, gh, monkeypatch, [False, False, True])
    assert left == [True] and reviews == [] and S.CHANGES_REQUESTED.label in gh.issues[n]["labels"]
    assert review.review_history(gh.comments(7))[-1]["verdict"] == review.TESTS_FAILED


def test_a_merge_gate_that_fails_once_then_passes_still_merges(pl, monkeypatch):
    p, gh = pl
    t, n, pr, finished = approved(p, gh, monkeypatch, contains=False)
    asked = failing_merges(gh, [])
    left = gates(p, monkeypatch, [False, True])
    p.merge_one(t, n, pr, "merge")
    assert left == [] and asked == [7] and len(finished) == 1 and p.git.pushes == 1


def test_a_merge_gate_that_fails_twice_sends_the_pr_back(pl, monkeypatch):
    p, gh = pl
    t, n, pr, finished = approved(p, gh, monkeypatch, contains=False)
    asked = failing_merges(gh, [])
    left = gates(p, monkeypatch, [False, False, True])
    p.merge_one(t, n, pr, "merge")
    assert left == [True] and asked == [] and finished == []
    assert S.CHANGES_REQUESTED.label in gh.issues[n]["labels"]


def test_a_batch_gate_that_fails_once_then_passes_merges_the_whole_batch(pl, tmp_path, monkeypatch):
    p, gh = pl
    runs = []
    order, seen = train_setup(p, gh, tmp_path, monkeypatch, THREE,
                              gate_ok=lambda files: (runs.append(1), len(runs) > 1)[1])
    assert p.merge_train(order, "merge") == "merged"
    assert len(seen["gates"]) == 2 and seen["gates"][0] == seen["gates"][1]  # the same tree, no bisect
    assert seen["merged"] == [20, 21, 22] and "batch_culprit" not in p.state.task("c/1.1")


# ------------------------------------------------------------------ 5. failure counters
def test_a_review_that_posts_its_verdict_clears_the_review_failure_count(pl, monkeypatch):
    p, gh = pl
    p.state.task("c/1.1")["fail_review"] = 1
    reviewing(p, gh, monkeypatch, [False, False])  # a rejection is a verdict too
    assert "fail_review" not in p.state.task("c/1.1")
    assert "fail_review" not in json.loads(p.state.path.read_text())["tasks"]["c/1.1"]


def test_a_review_crash_long_after_an_earlier_one_is_not_failed_twice(pl, monkeypatch):
    p, gh = pl
    p.bump_failure("c/1.1", "review", "boom")
    n, _, _ = reviewing(p, gh, monkeypatch, [True])
    gh.set_status(n, S.PENDING_REVIEW, S.APPROVED)
    p.bump_failure("c/1.1", "review", "boom")
    assert S.PENDING_REVIEW.label in gh.issues[n]["labels"] and p.state.task("c/1.1")["fail_review"] == 1
    p.bump_failure("c/1.1", "review", "boom")  # two in a row still go to Max
    assert S.NEEDS_DECISION.label in gh.issues[n]["labels"]


def test_a_merge_job_that_ends_normally_clears_the_merge_failure_count(pl, monkeypatch):
    p, gh = pl
    t, n, pr, finished = approved(p, gh, monkeypatch)
    p.state.task("c/1.1")["fail_merge"] = 1
    monkeypatch.setattr(p, "merge_one", lambda *a: None)  # e.g. sent back for a conflict
    p.job_merge(t, n, pr, "merge")
    assert "fail_merge" not in p.state.data["tasks"]["c/1.1"]
    assert "fail_merge" not in json.loads(p.state.path.read_text())["tasks"]["c/1.1"]


def test_a_merge_job_that_crashes_keeps_its_failure_count(pl, monkeypatch):
    p, gh = pl
    t, n, pr, _ = approved(p, gh, monkeypatch)
    p.state.task("c/1.1")["fail_merge"] = 1
    monkeypatch.setattr(p, "merge_one", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        p.job_merge(t, n, pr, "merge")
    assert p.state.task("c/1.1")["fail_merge"] == 1
