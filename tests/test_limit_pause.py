"""A usage limit pauses only until the known reset, even when that is sooner than the backoff."""

import time

from agentpipe.pipeline import message_reset
from test_pipeline import pl  # noqa: F401  (pytest fixture)


def test_a_known_reset_sooner_than_the_backoff_is_kept(pl):
    p, _ = pl
    p.cfg.limit_backoff_seconds = 1800
    now = time.time()
    p.state.data["rate"] = {"five_hour": {"utilization": 0.99, "resetsAt": now + 60, "status": "allowed"}}
    p.pause("You've hit your 5-hour limit")
    assert p.state.data["paused_until"] <= now + 125


def test_the_message_gives_the_reset_when_no_window_reading_does(pl):
    p, _ = pl
    p.cfg.limit_backoff_seconds = 1800
    soon = time.localtime(time.time() + 5 * 60)
    hour = soon.tm_hour % 12 or 12
    half = "pm" if soon.tm_hour >= 12 else "am"
    p.pause(f"You've hit your 5-hour limit · resets {hour}:{soon.tm_min:02d}{half} (Europe/Stockholm)")
    assert p.state.data["paused_until"] < time.time() + 7 * 60


def test_no_reset_known_falls_back_to_the_backoff(pl):
    p, _ = pl
    p.cfg.limit_backoff_seconds = 1800
    p.pause("overloaded")
    assert p.state.data["paused_until"] > time.time() + 1700


def test_message_reset_reads_12_hour_and_24_hour_times():
    noon = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1))
    assert time.localtime(message_reset("resets 7:10pm", noon))[3:5] == (19, 10)
    assert time.localtime(message_reset("resets 12am", noon))[3:5] == (0, 0)  # tomorrow
    assert message_reset("resets 12am", noon) > noon
    assert time.localtime(message_reset("resets at 23:30", noon))[3:5] == (23, 30)
    assert message_reset("no time here", noon) is None
