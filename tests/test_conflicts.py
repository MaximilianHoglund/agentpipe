"""Mechanical conflict resolution and the file-overlap helpers the scheduler uses."""

from pathlib import Path

import pytest

from agentpipe.conflicts import overlapping, resolve_additive, ticket_paths
from agentpipe.git import Git
from gitutil import commit_on, git, make_repo


def hunk(ours, base, theirs, eol=b"\n"):
    j = lambda ls: b"".join(l + eol for l in ls)
    return (b"<<<<<<< HEAD" + eol + j(ours) + b"||||||| base" + eol + j(base) + b"=======" + eol
            + j(theirs) + b">>>>>>> origin/dev" + eol)


def test_both_sides_adding_at_the_same_spot_keeps_both():
    text = b"mod a;\n" + hunk([b"mod b;"], [], [b"mod c;"]) + b"mod z;\n"
    assert resolve_additive(text) == b"mod a;\nmod b;\nmod c;\nmod z;\n"


def test_a_line_both_sides_added_is_kept_once():
    text = hunk([b"use x;", b"mod b;"], [], [b"use x;", b"mod c;"])
    assert resolve_additive(text) == b"use x;\nmod b;\nmod c;\n"


def test_crlf_files_keep_their_line_endings():
    text = b"a\r\n" + hunk([b"b"], [], [b"c"], eol=b"\r\n")
    assert resolve_additive(text) == b"a\r\nb\r\nc\r\n"


def test_every_hunk_is_resolved():
    text = hunk([b"1"], [], [b"2"]) + b"mid\n" + hunk([b"3"], [], [b"4"])
    assert resolve_additive(text) == b"1\n2\nmid\n3\n4\n"


@pytest.mark.parametrize("text", [
    hunk([b"x = 1"], [b"x = 0"], [b"x = 2"]),            # both changed the same line
    hunk([b"a"], [], [b"b"]) + hunk([b"c"], [b"old"], [b"d"]),  # one real hunk among additive ones
    b"<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n",          # no diff3 base section: can't tell
    b"<<<<<<< HEAD\na\n||||||| base\n=======\nb\n",       # unterminated
    b"no conflict here\n",
])
def test_anything_but_purely_additive_conflicts_is_left_alone(text):
    assert resolve_additive(text) is None


def test_overlap_counts_same_files_and_directories_but_not_ignored_ones():
    ignore = ["**/lib.rs", "docs/*"]
    assert overlapping(["crates/sim/src/run.rs"], ["crates/sim/src/run.rs"], ignore) == ["crates/sim/src/run.rs"]
    assert overlapping(["crates/sim/src"], ["crates/sim/src/run.rs"], ignore) == ["crates/sim/src"]
    assert overlapping(["./crates\\sim/src/run.rs"], ["crates/sim/src/"], ignore) == ["crates/sim/src/run.rs"]
    assert overlapping(["crates/sim/src/lib.rs", "docs/design.md"], ["crates/sim/src/lib.rs", "docs/design.md"],
                       ignore) == []
    assert overlapping(["lib.rs"], ["lib.rs"], ignore) == []  # `**/` also matches at the root
    assert overlapping(["crates/sim/src/run_x.rs"], ["crates/sim/src/run"], ignore) == []


def test_ticket_paths_are_the_files_its_body_names_without_urls():
    body = ("`crates/sim/src/run.rs:120` panics; see crates/sim/tests/run.rs and "
            "https://github.com/o/r/blob/dev/docs/x.md. Also crates/sim/src/run.rs:9 again.")
    assert ticket_paths(body) == ["crates/sim/src/run.rs", "crates/sim/tests/run.rs"]


# ------------------------------------------------------------------ against real git

@pytest.fixture
def repo(tmp_path: Path):
    return make_repo(tmp_path)


def test_git_merge_shows_the_ancestor_so_additive_conflicts_resolve(repo):
    commit_on(repo, "base", "lib.rs", "mod a;\nmod c;\nmod z;\n")
    commit_on(repo, "feature", "lib.rs", "mod a;\nmod b;\nmod z;\n")
    g = Git(repo)
    assert not g.merge(repo, "base")
    assert g.conflicted(repo) == ["lib.rs"]
    assert resolve_additive((repo / "lib.rs").read_bytes()) == b"mod a;\nmod b;\nmod c;\nmod z;\n"


def test_merge_commits_count_only_the_branchs_own_work(repo):
    g = Git(repo)
    commit_on(repo, "base", "lib.rs", "mod a;\nmod c;\nmod z;\n")
    commit_on(repo, "feature", "run.rs", "x = 1\n")
    approved = g.head(repo)
    assert g.merge(repo, "base")
    merges, others = g.merge_commits(repo, approved, "base")
    assert len(merges) == 1 and others == 0  # base's own commit isn't the branch's work
    assert g.remerge_diff(repo, merges[0]).strip() == ""
    commit_on(repo, "base", "run.rs", "x = 2\n")
    git(repo, "checkout", "-q", "feature")
    assert not g.merge(repo, "base")
    (repo / "run.rs").write_bytes(b"x = 3\n")
    git(repo, "commit", "-qam", "resolve")
    merges, others = g.merge_commits(repo, approved, "base")
    assert len(merges) == 2 and others == 0
    assert "x = 3" in g.remerge_diff(repo, merges[0])
    commit_on(repo, "feature", "lib.rs", "mod q;\n")
    assert g.merge_commits(repo, approved, "base")[1] == 1
