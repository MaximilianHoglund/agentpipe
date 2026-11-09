from agentpipe import proc
from agentpipe.github import GitHub


def test_comments_reads_paged_lines_without_slurp():
    calls = []

    def run(cmd, cwd, input=None):
        calls.append(cmd)
        out = ('{"body":"first","user":"max","created_at":"2026-11-07T10:00:00Z"}\n'
               '{"body":"line one\\nline two","user":"bot","created_at":"2026-11-07T11:00:00Z"}\n')
        return proc.Result(0, out, "")

    comments = GitHub("o/r", run=run).comments(5)

    assert "--slurp" not in calls[0]  # gh 2.45 (Ubuntu 24.04) has no --slurp
    assert "--paginate" in calls[0]
    assert comments == [
        {"body": "first", "user": "max", "created_at": "2026-11-07T10:00:00Z"},
        {"body": "line one\nline two", "user": "bot", "created_at": "2026-11-07T11:00:00Z"},
    ]


def _flaky(errors: list[str], out: str = "[]"):
    """A gh runner that fails with each of ``errors`` in turn, then succeeds."""
    calls = []

    def run(cmd, cwd, input=None):
        calls.append(cmd)
        if len(calls) <= len(errors):
            raise proc.CommandError(cmd, proc.Result(1, "", errors[len(calls) - 1]))
        return proc.Result(0, out, "")
    return run, calls


def test_network_blips_are_retried(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    tls = 'Post "https://api.github.com/graphql": net/http: TLS handshake timeout'
    run, calls = _flaky([tls, "read: connection reset by peer"])
    assert GitHub("o/r", run=run)._gh("pr", "list") == "[]"  # a read: any network error retries
    assert len(calls) == 3
    run, calls = _flaky([tls])
    GitHub("o/r", run=run)._gh("issue", "comment", "5", "--body", "x")  # never sent: safe to retry
    assert len(calls) == 2


def test_a_write_is_not_retried_once_it_may_have_landed(monkeypatch):
    import time
    import pytest
    monkeypatch.setattr(time, "sleep", lambda s: None)
    run, calls = _flaky(["read: connection reset by peer"])
    with pytest.raises(proc.CommandError):
        GitHub("o/r", run=run)._gh("issue", "comment", "5", "--body", "x")  # could post twice
    assert len(calls) == 1
    run, calls = _flaky(["HTTP 404: Not Found"])
    with pytest.raises(proc.CommandError):
        GitHub("o/r", run=run)._gh("pr", "view", "5")  # not a network error
    assert len(calls) == 1


def test_gives_up_after_the_last_retry(monkeypatch):
    import time
    import pytest
    monkeypatch.setattr(time, "sleep", lambda s: None)
    run, calls = _flaky(["TLS handshake timeout"] * 9)
    with pytest.raises(proc.CommandError):
        GitHub("o/r", run=run)._gh("pr", "list")
    assert len(calls) == len(GitHub.RETRY_WAITS) + 1


def test_reads_and_writes_tell_apart():
    from agentpipe.github import is_read
    assert is_read(("pr", "list")) and is_read(("issue", "view", "5")) and is_read(("api", "repos/o/r/issues"))
    assert not is_read(("api", "repos/o/r/issues/5/comments", "-f", "body=x"))  # -f makes it a POST
    assert not is_read(("api", "repos/o/r/labels", "-X", "DELETE"))
    assert not is_read(("issue", "edit", "5")) and not is_read(("pr", "merge", "5"))


def test_comments_on_an_issue_without_any():
    assert GitHub("o/r", run=lambda cmd, cwd, input=None: proc.Result(0, "", "")).comments(5) == []
