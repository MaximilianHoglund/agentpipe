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


def test_comments_on_an_issue_without_any():
    assert GitHub("o/r", run=lambda cmd, cwd, input=None: proc.Result(0, "", "")).comments(5) == []
