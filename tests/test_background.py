import sys
import time

from agentpipe import proc, review


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_background_collects_output_and_exit_code():
    b = proc.Background(py("import sys; print('hi'); sys.exit(3)"))
    r = b.result()
    assert r.code == 3
    assert r.out.strip() == "hi"


def test_background_times_out_like_run():
    r = proc.Background(py("import time; time.sleep(30)"), timeout=1).result()
    assert r.code == proc.TIMEOUT_CODE


def test_background_kill_stops_the_command():
    start = time.monotonic()
    b = proc.Background(py("import time; time.sleep(30)"))
    b.kill()
    assert time.monotonic() - start < 15
    assert b.p.poll() is not None


def h(verdict: str) -> dict:
    return {"verdict": verdict, "sha": "abc", "body": ""}


def test_resolution_review_possible_needs_an_approval_then_a_conflict():
    assert not review.resolution_review_possible([])
    assert not review.resolution_review_possible([h(review.REQUEST_CHANGES)])
    assert not review.resolution_review_possible([h(review.APPROVE)])
    assert not review.resolution_review_possible([h(review.CONFLICT), h(review.APPROVE)])
    assert review.resolution_review_possible([h(review.APPROVE), h(review.CONFLICT)])
    assert review.last_approval([h(review.APPROVE), h(review.CONFLICT), h(review.APPROVE)]) == 2
