"""Which OpenSpec tasks an agent-filed ticket needs merged before it can be worked on.

Tickets aren't planned, so their blockers come from their own text: task keys it names
(`combat-simulation/1.4`), bare task ids (`3.3`) when the ticket names exactly one change,
and the task whose implementer filed it, since a ticket usually builds on that task's code.
"""

from __future__ import annotations

import re

FULL_KEY = re.compile(r"(?<![\w/.-])([a-z][a-z0-9-]*)/(\d+\.\d+)(?![\w]|\.\d)")
# A bare id, but not part of a version such as 1.98.0 or 0.5.5.
BARE_ID = re.compile(r"(?<![\w/.-])(\d+\.\d+)(?![\w]|\.\d)")
SOURCE = re.compile(r"from the (?:implementer|reviewer) of ([a-z][a-z0-9-]*/\d+\.\d+)")


FILED_FROM = re.compile(r"Filed by the pipeline from the [a-z ]+? of ([a-z][a-z0-9-]*/\d+(?:\.\d+[a-z]*)?)")


def filed_from(body: str) -> str | None:
    """The task or ticket key whose agent filed this ticket (``combat-simulation/2.1`` or
    ``ticket/361``), from the pipeline's footer, or None."""
    m = FILED_FROM.search(body)
    return m.group(1) if m else None


def ticket_blockers(body: str, task_keys: set[str]) -> set[str]:
    """Task keys (from ``task_keys``, the target's OpenSpec tasks) that ``body`` depends on."""
    found = {f"{c}/{n}" for c, n in FULL_KEY.findall(body)}
    found |= set(SOURCE.findall(body))
    changes = {k.split("/")[0] for k in found if k in task_keys}
    changes |= {c for c in {k.split("/")[0] for k in task_keys} if re.search(rf"\b{re.escape(c)}\b", body)}
    if len(changes) == 1:
        (change,) = changes
        scrubbed = FULL_KEY.sub(" ", body)
        found |= {f"{change}/{n}" for n in BARE_ID.findall(scrubbed)}
    return found & task_keys
