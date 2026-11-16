"""Agent output schemas, verdict rules, and the comments the pipeline posts."""

from __future__ import annotations

from string import Template
from importlib import resources

from agentpipe.status import BOT_MARKER, REVIEW_MARKER, is_bot_comment, parse_review_marker

CATEGORIES = ["spec", "correctness", "corner-cut", "duplication", "architecture",
              "test-behaviour", "test-coverage", "docs", "other"]

TICKETS_PROPERTY = {
    "type": "array",
    "description": "Defects or improvements noticed outside this task's scope, to file as new tickets",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["defect", "improvement"]},
            "title": {"type": "string"},
            "body": {"type": "string", "description": "What, where (file:line), why it matters, suggested fix"},
        },
        "required": ["kind", "title", "body"],
    },
}

PREFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "command": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "output": {"type": "string"},
                },
                "required": ["tool", "command", "exit_code"],
            },
        }
    },
    "required": ["results"],
}

IMPLEMENTER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "incomplete", "needs_decision"]},
        "summary": {"type": "string"},
        "tests": {"type": "string"},
        "question": {"type": "string"},
        "docs": {"type": "string",
                 "description": "Docs updated and what each now records, or 'none' and why nothing needed it"},
        "tickets": TICKETS_PROPERTY,
        "probes": {
            "type": "array",
            "description": "Deliberate breaks you made in your own change, each caught by a test",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "description": "The rule the break tests, as the docs or spec state it"},
                    "break": {"type": "string", "description": "file:line and what you changed"},
                    "caught_by": {"type": "string", "description": "The test that failed, by name"},
                },
                "required": ["rule", "break", "caught_by"],
            },
        },
        "no_probes": {"type": "string",
                      "description": "Only when the change states no rule a test could catch: why"},
    },
    "required": ["status", "summary", "tests", "docs"],
}

# Probes an implementer reports before its `done` goes to review (claude.RunState's report check).
# 2026-11-16: 22 of 36 blocking findings in a day were reviewer breaks no test caught, though the
# prompt already asked implementers to probe; reporting them makes the probing checkable.
MIN_PROBES = 3


def probe_gap(out: dict) -> str | None:
    """Why an implementer's `done` report may not go out yet (too few probes with the test that
    caught each, and no reason given for having none), or None."""
    if not isinstance(out, dict) or out.get("status") != "done":
        return None
    if str(out.get("no_probes") or "").strip():
        return None
    probes = [p for p in out.get("probes") or [] if isinstance(p, dict)]
    caught = [p for p in probes if str(p.get("caught_by") or "").strip() and str(p.get("break") or "").strip()]
    if len(caught) >= MIN_PROBES:
        return None
    missing = [str(p.get("rule") or p.get("break") or "?") for p in probes if p not in caught]
    return (
        f"Not yet: your report lists {len(caught)} deliberate-break probe(s) with the test that caught "
        f"each, and review expects at least {MIN_PROBES}. Reviewers reject about 4 PRs in 10, mostly "
        "for a break no test catches: a rule the docs, design or doc comments state (which value "
        "wins, what is captured when, such as a snapshot taken at launch versus live state, the "
        "order of steps), and error paths (every new error or report branch). Commit your work, "
        "then for each such rule in your change break it, run the tests, add a test when none "
        "fails, `git restore` the break, and list it in `probes`. "
        + (f"These have no catching test yet: {'; '.join(missing)}. " if missing else "")
        + "If your change states no rule a test could catch (docs or config only), give the reason "
        "in `no_probes` instead. Then return the structured output again.")


def probe_section(out: dict) -> str:
    """The PR body's account of the implementer's probes, for the reviewer."""
    probes = [p for p in out.get("probes") or [] if isinstance(p, dict)]
    if not probes:
        reason = str(out.get("no_probes") or "").strip()
        return f"none: {reason}" if reason else "none reported"
    return "\n".join(f"- {p.get('rule', '?')}: broke `{p.get('break', '?')}`, caught by "
                     f"`{p.get('caught_by') or 'nothing'}`" for p in probes)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "nit"]},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "problem": {"type": "string"},
                    "required_change": {"type": "string"},
                },
                "required": ["severity", "category", "file", "problem", "required_change"],
            },
        },
        "probes": {
            "type": "array",
            "description": "Deliberate breaks made to test the tests",
            "items": {
                "type": "object",
                "properties": {
                    "mutation": {"type": "string"},
                    "caught": {"type": "boolean"},
                    "equivalent": {"type": "boolean",
                                   "description": "true if the break cannot change observable behaviour"},
                    "by": {"type": "string"},
                },
                "required": ["mutation", "caught"],
            },
        },
        "tickets": TICKETS_PROPERTY,
    },
    "required": ["verdict", "summary", "findings", "probes"],
}

# Verdicts written in review markers. Only request_changes and tests_failed use up a
# round; a merge conflict is nobody's fault and does not.
APPROVE, REQUEST_CHANGES, TESTS_FAILED, CONFLICT = "approve", "request_changes", "tests_failed", "conflict"
COUNTED = {REQUEST_CHANGES, TESTS_FAILED}


def prompt(name: str, **values) -> str:
    text = resources.files("agentpipe").joinpath(f"prompts/{name}.md").read_text(encoding="utf-8")
    return Template(text).substitute(values)


def normalize_verdict(raw: dict) -> dict:
    """Enforce the verdict rule regardless of what the reviewer claimed.

    Any blocking finding forces request_changes. A probe (deliberate break) that the
    tests missed, and that the reviewer did not judge behaviour-preserving, is a
    coverage gap: without a blocking test-coverage finding already covering it, one
    is added.
    """
    findings = [dict(f) for f in raw.get("findings", [])]
    for f in findings:
        if f.get("category") not in CATEGORIES:
            f["category"] = "other"
        if f.get("severity") not in ("blocking", "nit"):
            f["severity"] = "blocking"
    probes = list(raw.get("probes", []))
    uncaught = [p for p in probes if not p.get("caught") and not p.get("equivalent")]
    if uncaught and not any(f["category"] == "test-coverage" and f["severity"] == "blocking" for f in findings):
        findings.append({
            "severity": "blocking", "category": "test-coverage", "file": "", "line": 0,
            "problem": "Deliberate breaks went undetected by the tests: "
                       + "; ".join(p.get("mutation", "?") for p in uncaught),
            "required_change": "Add tests that fail under each of these breaks.",
        })
    verdict = REQUEST_CHANGES if any(f["severity"] == "blocking" for f in findings) else APPROVE
    return {"verdict": verdict, "summary": raw.get("summary", ""), "findings": findings, "probes": probes}


def render_review(v: dict, sha: str, round_: int, max_rounds: int) -> str:
    head = {APPROVE: "✅ Approved — merging", REQUEST_CHANGES: "❌ Changes requested",
            TESTS_FAILED: "❌ Test gate failed", CONFLICT: "⚠️ Conflicts with the base branch"}[v["verdict"]]
    counted = f" (round {round_}/{max_rounds})" if v["verdict"] in COUNTED else ""
    lines = [REVIEW_MARKER.format(sha=sha, verdict=v["verdict"], round=round_),
             f"### Pipeline review: {head}{counted}", "", f"Reviewed commit `{sha[:10]}`.", ""]
    if v.get("summary"):
        lines += [v["summary"], ""]
    for sev in ("blocking", "nit"):
        fs = [f for f in v.get("findings", []) if f["severity"] == sev]
        if not fs:
            continue
        lines.append(f"**{'Blocking' if sev == 'blocking' else 'Nits'}**")
        lines.append("")
        for i, f in enumerate(fs, 1):
            loc = f["file"] + (f":{f['line']}" if f.get("line") else "") if f.get("file") else ""
            where = f" `{loc}`" if loc else ""
            lines.append(f"{i}. [{f['category']}]{where} {f['problem']}")
            lines.append(f"   - **Required:** {f['required_change']}")
        lines.append("")
    if v.get("probes"):
        lines += ["<details><summary>Test probes</summary>", ""]
        for p in v["probes"]:
            mark = ("caught" if p.get("caught") else
                    "not caught (behaviour-preserving)" if p.get("equivalent") else "**not caught**")
            by = f" by `{p['by']}`" if p.get("by") else ""
            lines.append(f"- {p['mutation']} — {mark}{by}")
        lines += ["", "</details>"]
    if v.get("details"):
        lines += ["", "<details><summary>Output</summary>", "", "```", v["details"], "```", "", "</details>"]
    return "\n".join(lines).rstrip() + "\n"


# A PR comment holding this gives the PR a fresh set of review rounds (`agentpipe rounds-reset`).
ROUNDS_RESET_MARKER = "<!-- pipeline-rounds-reset -->"


def review_history(comments: list[dict]) -> list[dict]:
    """Pipeline review markers on a PR, oldest first, each with its comment body and time.
    Reviews before the latest rounds reset stay (revise still reads their feedback) but are
    marked ``counts: False``, so they use up no round."""
    out = []
    for c in comments:
        if ROUNDS_RESET_MARKER in (c.get("body") or ""):
            for h in out:
                h["counts"] = False
        m = parse_review_marker(c["body"])
        if m:
            out.append({**m, "body": c["body"], "at": c.get("created_at", ""), "counts": True})
    return out


def rounds_used(history: list[dict]) -> int:
    """Rejections counted against `max_rounds`. An approved PR that then conflicts with the base
    starts a fresh count: what its update gets rejected for is fallout from the merge, not the
    work the earlier rounds judged, and counting it sent approved work to a decision
    (2026-11-15: save-system/1.4 approved, then rejected at round 2/3 for its merge from dev)."""
    start = 0
    for i, h in enumerate(history):
        if h["verdict"] == CONFLICT and last_approval(history[:i]) is not None:
            start = i
    return sum(1 for h in history[start:] if h["verdict"] in COUNTED and h.get("counts", True))


def last_approval(history: list[dict]) -> int | None:
    """Index of the latest approval in `history`, if any."""
    return next((i for i in range(len(history) - 1, -1, -1) if history[i]["verdict"] == APPROVE), None)


def resolution_review_possible(history: list[dict]) -> bool:
    """True when the PR was approved and has since hit a merge conflict: then a review of the
    conflict resolution alone may do, if the branch only merged the base since the approval."""
    approved = last_approval(history)
    return approved is not None and any(h["verdict"] == CONFLICT for h in history[approved + 1:])


def human_comments(comments: list[dict]) -> list[dict]:
    return [c for c in comments if not is_bot_comment(c["body"])]


def bot(text: str) -> str:
    return f"{BOT_MARKER}\n{text}"
