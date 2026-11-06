"""The dispatcher's review line says the outcome in words."""

from agentpipe import review
from agentpipe.pipeline import review_line

WHAT = 'c/1.1 "Add a combat log"'


def test_approved_review_says_approved_and_where_it_goes():
    v = {"verdict": review.APPROVE, "summary": "Looks good.", "findings": [{"severity": "nit"}]}
    assert review_line(WHAT, 7, v, 1, 3) == (
        'review c/1.1 "Add a combat log" PR #7: APPROVED (round 1/3, 1 nit); to merge. Looks good.')


def test_rejected_review_counts_blocking_findings():
    v = {"verdict": review.REQUEST_CHANGES, "summary": "",
         "findings": [{"severity": "blocking"}, {"severity": "blocking"}, {"severity": "nit"}]}
    assert review_line(WHAT, 7, v, 2, 3) == (
        'review c/1.1 "Add a combat log" PR #7: REJECTED, changes requested '
        '(round 2/3, 2 blocking, 1 nit); back to the implementer')


def test_last_round_rejection_says_it_needs_a_decision():
    v = {"verdict": review.TESTS_FAILED, "summary": "`cargo test` failed on this commit.", "findings": []}
    assert "REJECTED, test gate failed (round 3/3); out of rounds, needs a decision" in review_line(WHAT, 7, v, 3, 3)
