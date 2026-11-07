"""Ticket triage: technical proposals are approved without Max, the rest wait for him."""

from agentpipe.claude import ClaudeResult
from agentpipe.status import TICKET_MARKER, Status
from agentpipe.tasks import Task
from test_pipeline import pl  # noqa: F401  (pytest fixture)


def proposals(p, gh):
    issues = {}
    for status in (Status.PENDING_APPROVAL, Status.PENDING_APPROVAL, Status.PENDING_APPROVAL,
                   Status.PENDING_IMPLEMENTATION):
        n = gh.add(TICKET_MARKER + "\nbody", status)
        k = f"ticket/{n}"
        issues[k] = dict(gh.issues[n])
        p.tasks[k] = Task("ticket", str(n), f"title {n}", False, "improvement", 0)
    p.ticket_bodies = {k: i["body"] for k, i in issues.items()}
    return issues


class Triage:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
        self.calls.append((agent.model, prompt))
        return ClaudeResult(True, "s", "", {"tickets": self.answers}, False, subtype="success")


def test_triage_approves_technical_and_leaves_the_rest_once(pl):
    p, gh = pl
    p.t.auto_approve = True
    issues = proposals(p, gh)
    a, b, c, d = sorted(issues, key=lambda k: int(k.split("/")[1]))
    p.state.task(c)["runs"] = 1  # already worked on: not a proposal
    p.claude = Triage([{"key": a, "verdict": "approve", "reason": "Test infra only."},
                       {"key": b, "verdict": "ask", "reason": "Changes unit targeting."}])
    p.triage_tickets(gh, issues)
    p.triage_tickets(gh, issues)  # nothing new: no second run
    assert len(p.claude.calls) == 1
    prompt = p.claude.calls[0][1]
    assert a in prompt and b in prompt and c not in prompt and d not in prompt
    na, nb = issues[a]["number"], issues[b]["number"]
    assert Status.PENDING_IMPLEMENTATION.label in gh.issues[na]["labels"]
    assert Status.PENDING_IMPLEMENTATION.label in issues[a]["labels"]  # rate_tickets sees it this sync
    assert Status.PENDING_APPROVAL.label in gh.issues[nb]["labels"]
    assert "Approved by triage" in gh.comments(na)[0]["body"]
    assert "Changes unit targeting." in gh.comments(nb)[0]["body"]


def test_triage_is_off_by_default_and_skips_parked(pl):
    p, gh = pl
    issues = proposals(p, gh)
    p.claude = None  # would fail if called
    p.triage_tickets(gh, issues)
    p.t.auto_approve = True
    p.t.parked = [i["number"] for i in issues.values()]
    p.triage_tickets(gh, issues)


def test_failed_triage_is_retried_next_sync(pl):
    p, gh = pl
    p.t.auto_approve = True
    issues = proposals(p, gh)

    class Fail:
        calls = 0

        def run(self, *a, **k):
            Fail.calls += 1
            return ClaudeResult(False, "s", "", None, False, subtype="error_max_turns")

    p.claude = Fail()
    p.triage_tickets(gh, issues)
    p.triage_tickets(gh, issues)
    assert Fail.calls == 2 and not p.state.data.get("triaged")
