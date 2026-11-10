"""What an approved ticket waits for: the rating run's answer, or the guess from its text."""

import dataclasses

from agentpipe.claude import ClaudeResult
from agentpipe.config import Agent
from agentpipe.status import TICKET_MARKER, Status
from agentpipe.tasks import Task
from test_pipeline import pl  # noqa: F401  (pytest fixture)

FULL, LIGHT = "opus", "sonnet"
KEY = "ticket/7"
MENTIONS = f"{TICKET_MARKER}\nc/1.1 broke the export; unlike c/1.2, this needs no new format."


class Rater:
    """Answers the rating run with ``answers`` (ticket key -> entry fields) and keeps the prompts."""

    def __init__(self, answers, ok=True):
        self.answers, self.ok, self.prompts = answers, ok, []

    def run(self, prompt, cwd, agent, schema, *, log_name, resume=None, env=None):
        self.prompts.append(prompt)
        if not self.ok:
            return ClaudeResult(False, "s", "", None, False, subtype="error")
        rows = [{"key": k, "tier": "standard", "unblocks": False, **e} for k, e in self.answers.items()]
        return ClaudeResult(True, "s", "", {"tickets": rows}, False, subtype="success")


def ticket(p, body=MENTIONS, key=KEY, light=True):
    """An approved, unstarted ticket; returns the issues ``rate_tickets`` takes."""
    p.cfg.agents["implementer"] = Agent(FULL, 1, [], light_model=LIGHT if light else "")
    p.cfg.agents["planner"] = Agent(FULL, 1, [], light_model=LIGHT)
    n = int(key.split("/")[1])
    p.tasks[key] = Task("ticket", str(n), f"title {n}", False, "defect", 0)
    p.ticket_bodies = {**getattr(p, "ticket_bodies", {}), key: body}
    return {key: {"number": n, "state": "OPEN", "labels": [Status.PENDING_IMPLEMENTATION.label], "body": body}}


def finish(p, key):
    p.tasks[key] = dataclasses.replace(p.tasks[key], done=True)


def test_an_unrated_ticket_waits_for_the_tasks_its_text_names(pl):
    p, _ = pl
    ticket(p)
    assert p.ticket_waits_for(KEY) == ["c/1.1", "c/1.2"]
    assert sorted(p.exclusive_needs(KEY)) == ["c/1.1", "c/1.2"]


def test_the_rated_answer_replaces_the_guess_and_only_its_unfinished_tasks_wait(pl):
    p, _ = pl
    issues = ticket(p)
    finish(p, "c/1.4")
    p.claude = Rater({KEY: {"waits_for": ["c/1.3", "c/1.4"]}})
    p.rate_tickets(issues)
    assert p.state.task(KEY)["needs"] == ["c/1.3", "c/1.4"]
    assert p.ticket_waits_for(KEY) == ["c/1.3"]  # not the mentioned 1.1 and 1.2, not the merged 1.4
    assert p.exclusive_needs(KEY) == ["c/1.3"]
    finish(p, "c/1.3")
    assert p.ticket_waits_for(KEY) == [] and p.exclusive_needs(KEY) == []


def test_a_ticket_that_only_mentions_tasks_waits_for_nothing_once_rated(pl):
    p, _ = pl
    issues = ticket(p)
    p.claude = Rater({KEY: {"waits_for": []}})
    p.rate_tickets(issues)
    assert p.state.task(KEY)["needs"] == []
    assert p.ticket_waits_for(KEY) == [] and p.exclusive_needs(KEY) == []


def test_the_answer_keeps_only_known_tasks_that_are_not_tickets(pl):
    p, _ = pl
    issues = ticket(p)
    issues.update(ticket(p, "another", "ticket/8"))
    p.claude = Rater({KEY: {"waits_for": ["c/1.2", " c/1.3 ", "ticket/8", KEY, "other/9.9", 4]},
                      "ticket/8": {"waits_for": []}})
    p.rate_tickets(issues)
    assert p.state.task(KEY)["needs"] == ["c/1.2", "c/1.3"]
    assert p.ticket_waits_for(KEY) == ["c/1.2", "c/1.3"]


def test_a_stored_task_that_has_since_left_the_changes_is_not_waited_for(pl):
    p, _ = pl
    ticket(p)
    p.state.task(KEY)["needs"] = ["c/1.2", "gone/1.1"]
    assert p.ticket_waits_for(KEY) == ["c/1.2"] and p.exclusive_needs(KEY) == ["c/1.2"]


def test_the_rater_is_given_the_unfinished_tasks_and_no_tickets(pl):
    p, _ = pl
    issues = ticket(p, f"{TICKET_MARKER}\nplain body")
    issues.update(ticket(p, "another", "ticket/8"))
    finish(p, "c/1.4")
    p.claude = Rater({})
    p.rate_tickets(issues)
    tasks = p.claude.prompts[0].split("## Open tasks")[1].split("## Answer")[0]
    assert "- c/1.1: a" in tasks and "- c/1.3: c" in tasks
    assert "c/1.4" not in tasks and "ticket/" not in tasks


def test_the_guess_stays_when_the_run_fails_or_leaves_the_answer_out(pl):
    p, _ = pl
    issues = ticket(p)
    p.claude = Rater({}, ok=False)
    p.rate_tickets(issues)
    assert "needs" not in p.state.task(KEY)
    assert p.ticket_waits_for(KEY) == ["c/1.1", "c/1.2"]
    p.tiers_tried.clear()  # a restart
    p.claude = Rater({KEY: {}})  # rated, but without `waits_for`
    p.rate_tickets(issues)
    assert p.state.task(KEY)["tier"] == "standard" and "needs" not in p.state.task(KEY)
    assert sorted(p.exclusive_needs(KEY)) == ["c/1.1", "c/1.2"]


def test_the_guess_stays_without_a_light_model(pl):
    p, _ = pl
    issues = ticket(p, light=False)
    p.claude = None  # would fail if called
    p.rate_tickets(issues)
    assert p.ticket_waits_for(KEY) == ["c/1.1", "c/1.2"]


def test_tickets_rated_before_the_answer_existed_are_rated_again_once(pl):
    p, _ = pl
    issues = ticket(p)
    issues.update(ticket(p, MENTIONS, "ticket/9"))
    for k in issues:
        p.state.task(k).update(tier="light", unblocks=False, unblocks_rule=2)
    p.state.task("ticket/9")["runs"] = 1  # started: it no longer waits, so it isn't asked about
    p.claude = Rater({KEY: {"waits_for": ["c/1.2"], "tier": "standard"}})
    p.rate_tickets(issues)
    p.rate_tickets(issues)
    assert len(p.claude.prompts) == 1
    assert f"### {KEY}:" in p.claude.prompts[0] and "### ticket/9:" not in p.claude.prompts[0]
    assert p.state.task(KEY)["needs"] == ["c/1.2"] and p.state.task(KEY)["tier"] == "light"
    assert "needs" not in p.state.task("ticket/9")
    p.tiers_tried.clear()  # a restart: it has its answer, so nothing is asked
    p.rate_tickets(issues)
    assert len(p.claude.prompts) == 1


def test_a_rated_ticket_that_mentions_an_open_task_starts_without_waiting(pl):
    p, gh = pl
    n = gh.add(MENTIONS, Status.PENDING_IMPLEMENTATION, extra=("kind: defect",))
    key = f"ticket/{n}"
    p.state.data["tickets"] = [n]
    p.load_tickets(p.issues_by_key(gh.list_issues()))
    p.state.task(key)["needs"] = []
    p.tick()
    assert key in {k for _, k, _ in p.started}
    assert gh.issues[n]["labels"][-1] != Status.AWAITING_TASK.label


def test_an_exclusive_ticket_does_not_pull_in_a_task_it_only_mentions(pl):
    from test_pipeline import task_issue
    p, gh = pl
    p.t.exclusive = ["plan-x"]
    task_issue(gh, "c/1.1", Status.IN_PROGRESS)  # mentioned by the ticket
    task_issue(gh, "c/1.2", Status.IN_PROGRESS)  # needed by it
    n = gh.add(MENTIONS, Status.PENDING_IMPLEMENTATION)
    key = f"ticket/{n}"
    p.tasks[key] = Task("ticket", str(n), "Plan x", False, "improvement", 0)
    p.state.task(key).update(origin="plan-x", needs=["c/1.2"])
    p.issues_by_key = lambda raw: {(f"c/1.{i['number']}" if i["number"] < 3 else f"ticket/{i['number']}"): i
                                   for i in raw}
    p.load_tickets = lambda issues: p.ticket_bodies.update({key: gh.issues[n]["body"]})
    p.ticket_bodies = {key: gh.issues[n]["body"]}
    p.register_wait = lambda waiter, blockers, old=None: None
    p.tick()
    started = [s[1] for s in p.started]
    assert "c/1.2" in started and "c/1.1" not in started
