"""One-off problems are watched rather than worked on, until they recur; unblocking tickets need
evidence and hold at most a capped number of implementer slots."""

from agentpipe import pipeline as pipeline_mod
from agentpipe.claude import ClaudeResult
from agentpipe.status import TASK_MARKER, TICKET_MARKER, Status
from agentpipe.tasks import Task
from test_pipeline import pl, task_issue  # noqa: F401  (pytest fixture)
from test_tiers import LIGHT, tiered

S = Status


class Answers:
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
        self.prompts.append(prompt)
        return ClaudeResult(True, "s", "", {"tickets": self.answers.pop(0)}, False, subtype="success")


def ticket(p, gh, status=S.PROPOSED, title="t"):
    n = gh.add(TICKET_MARKER + f"\nbody of {title}", status, title=title)
    k = f"ticket/{n}"
    p.state.data.setdefault("tickets", []).append(n)
    p.tasks[k] = Task("ticket", str(n), title, False, "defect", 0)
    p.ticket_bodies[k] = gh.issues[n]["body"]
    return k


def triage_setup(p, gh):
    p.t.auto_approve = True
    p.ticket_bodies = {}
    gh.close_issue = lambda n: gh.issues[n].__setitem__("state", "CLOSED")


def current(p, gh):
    return {k: i for k, i in p.issues_by_key(gh.list_issues()).items() if k in p.tasks}


def test_a_one_off_problem_is_watched_not_worked_on(pl):
    p, gh = pl
    triage_setup(p, gh)
    k = ticket(p, gh, title="fps test failed once under load")
    p.claude = Answers([{"key": k, "verdict": "watch", "reason": "Seen once under load."}])
    p.triage_tickets(gh, current(p, gh))
    n = int(k.split("/")[1])
    assert S.WATCHING.label in gh.issues[n]["labels"]
    assert p.state.task(k)["sightings"] == 1 and "Watching" in gh.comments(n)[-1]["body"]
    p.tick()
    assert k not in [s[1] for s in p.started]  # not scheduled
    assert n not in p.state.data.get("awaiting_input", [])  # not waiting on Max


def test_sightings_close_the_new_ticket_and_promote_the_watched_one_once_it_recurs(pl):
    p, gh = pl
    triage_setup(p, gh)
    w = ticket(p, gh, title="fps test flaky")
    p.claude = Answers([{"key": w, "verdict": "watch", "reason": "once"}])
    p.triage_tickets(gh, current(p, gh))
    wn = int(w.split("/")[1])
    for i in range(2, p.t.watch_promote_at + 1):
        k = ticket(p, gh, title=f"60 fps check failed again {i}")
        p.claude = Answers([{"key": k, "verdict": "recurs", "same_as": w, "reason": "Same fps check."}])
        p.triage_tickets(gh, current(p, gh))
        assert "watched" in p.claude.prompts[0] and "fps test flaky" in p.claude.prompts[0]
        assert gh.issues[int(k.split("/")[1])]["state"] == "CLOSED"
        assert p.state.task(w)["sightings"] == i
        promoted = S.PENDING_IMPLEMENTATION.label in gh.issues[wn]["labels"]
        assert promoted == (i == p.t.watch_promote_at)
    assert "Promoted" in gh.comments(wn)[-1]["body"]


def test_a_sighting_of_an_unknown_problem_is_watched_itself(pl):
    p, gh = pl
    triage_setup(p, gh)
    k = ticket(p, gh)
    p.claude = Answers([{"key": k, "verdict": "recurs", "same_as": "ticket/999", "reason": "?"}])
    p.triage_tickets(gh, current(p, gh))
    assert S.WATCHING.label in gh.issues[int(k.split("/")[1])]["labels"]


def rated(p, gh, unblocks_answer):
    tiered(p)
    p.ticket_bodies = {}
    k = ticket(p, gh, S.PENDING_IMPLEMENTATION)
    p.claude = Answers([{"key": k, "tier": "standard", "unblocks": unblocks_answer}])
    return k


def test_tickets_rated_unblocking_under_the_old_rule_are_rated_again(pl):
    p, gh = pl
    k = rated(p, gh, False)
    p.state.task(k).update(unblocks=True, tier="standard")  # old rating, no rule version
    p.rate_tickets(current(p, gh))
    assert p.state.task(k)["unblocks"] is False
    assert p.state.task(k)["unblocks_rule"] == pipeline_mod.UNBLOCKS_RULE


def test_a_started_ticket_keeps_its_old_rating(pl):
    p, gh = pl
    k = rated(p, gh, False)
    p.state.task(k).update(unblocks=True, tier="standard", runs=1)
    p.claude = None  # would fail if called
    p.rate_tickets(current(p, gh))
    assert p.state.task(k)["unblocks"] is True


def test_a_promoted_tickets_rating_shows_its_sightings(pl):
    p, gh = pl
    k = rated(p, gh, True)
    p.state.task(k)["sightings"] = 3
    p.rate_tickets(current(p, gh))
    assert "seen 3 times" in p.claude.prompts[0] and p.state.task(k)["unblocks"] is True


def test_unblocking_tickets_hold_at_most_their_capped_slots(pl):
    p, gh = pl
    p.ticket_bodies = {}
    a, b = ticket(p, gh, S.PENDING_IMPLEMENTATION, "a"), ticket(p, gh, S.PENDING_IMPLEMENTATION, "b")
    for k in (a, b):
        p.state.task(k).update(unblocks=True, unblocks_rule=pipeline_mod.UNBLOCKS_RULE, tier="standard")
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    task_issue(gh, "c/1.1", S.PENDING_IMPLEMENTATION)
    p.tick()
    started = [s[1] for s in p.started]
    assert sum(k in started for k in (a, b)) == 1 and "c/1.1" in started
    p.t.unblocking_slots = 2
    p.jobs.pop("c/1.1")
    p.tick()
    assert a in [s[1] for s in p.started] and b in [s[1] for s in p.started]
