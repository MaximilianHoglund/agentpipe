from agentpipe.tickets import ticket_blockers

KEYS = {"project-foundation/1.1", "project-foundation/1.2", "project-foundation/3.3",
        "combat-simulation/1.2", "combat-simulation/1.4", "combat-simulation/3.3"}
FOOTER = "\n\n---\n_Filed by the pipeline from the implementer of {src} (#36). Approve by setting ..._\n"


def test_full_keys_and_bare_ids_within_the_one_named_change():
    # Ticket #216: names project-foundation/1.2 in full and 3.3 bare.
    body = ("It needs files from project-foundation/1.2 (scripts/tests/setup.*) and from 3.3 "
            "(scripts/check.ps1, not started).") + FOOTER.format(src="ticket/206")
    assert ticket_blockers(body, KEYS) == {"project-foundation/1.2", "project-foundation/3.3"}


def test_the_filing_task_is_a_blocker():
    # Ticket #212: builds on code from the task whose implementer filed it.
    body = "Since combat-simulation/1.4, results depend on the content version." + \
        FOOTER.format(src="combat-simulation/1.4")
    assert ticket_blockers(body, KEYS) == {"combat-simulation/1.4"}


def test_versions_and_unknown_or_ambiguous_ids_are_ignored():
    body = ("Godot 4.7.2, rust 1.98.0 and the godot crate 0.5.5; see 3.3 and 1.2, "
            "and other-change/9.9.")
    assert ticket_blockers(body, KEYS) == set()  # no single change named: bare ids are ambiguous
    body2 = "combat-simulation needs 1.98.0 and 3.3 first"
    assert ticket_blockers(body2, KEYS) == {"combat-simulation/3.3"}
