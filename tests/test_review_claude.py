"""parse_output: runs cut short before their result event, and what counts as a usage limit."""

import json

import pytest

from agentpipe import proc
from agentpipe.claude import CAPPED, LIMIT_RE, TIMEOUT, parse_output
from agentpipe.pipeline import message_reset

RATE = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":'
        '{"five_hour":{"utilization":0.5,"resetsAt":1793797800,"status":"allowed"}}}}')
STREAM = "\n".join([
    '{"type":"system","subtype":"init","session_id":"s7"}',
    RATE,
    '{"type":"assistant","session_id":"s7","message":{"content":[{"type":"text","text":"working"}]}}',
    '{"type":"user","session_id":"s7","message":{"content":[]}}',
])


def test_timeout_with_stream_events_is_a_timeout():
    res = parse_output(STREAM, "timed out after 5s", proc.TIMEOUT_CODE)
    assert res.subtype == TIMEOUT and res.subtype in CAPPED
    assert (res.ok, res.limited, res.session_id) == (False, False, "s7")
    assert res.text == "timed out after 5s"
    assert res.rate["five_hour"]["utilization"] == 0.5


def test_timeout_is_never_limited_and_needs_no_session():
    res = parse_output('{"type":"system","subtype":"init"}', "timed out after 5s; usage limit",
                       proc.TIMEOUT_CODE)
    assert (res.subtype, res.limited, res.session_id) == (TIMEOUT, False, None)
    bare = parse_output("", "", proc.TIMEOUT_CODE)
    assert bare.subtype == TIMEOUT and bare.text


def test_timeout_after_the_result_event_keeps_the_result():
    out = STREAM + '\n{"type":"result","subtype":"success","is_error":false,"result":"done",' \
                   '"session_id":"s7","structured_output":{"a":1}}'
    res = parse_output(out, "timed out after 5s", proc.TIMEOUT_CODE)
    assert (res.ok, res.subtype, res.structured) == (True, "success", {"a": 1})


def test_run_cut_short_reports_stderr_not_a_stream_event():
    res = parse_output(STREAM, "Error: something broke", 1)
    assert (res.ok, res.subtype, res.limited) == (False, "", False)
    assert res.text == "Error: something broke"
    assert res.session_id == "s7" and res.structured is None
    silent = parse_output(STREAM, "", 3)
    assert not silent.ok and "exit 3" in silent.text
    # A subtype on a stream event ("init") is not the run's subtype.
    assert parse_output('{"type":"system","subtype":"success","structured_output":{"a":1}}', "", 0).ok is False


def test_run_cut_short_by_a_limit_is_limited():
    res = parse_output(STREAM, "API Error: 529 Overloaded", 1)
    assert res.limited and not res.ok and res.session_id == "s7"
    assert not parse_output(STREAM, "inventory capacity check failed", 1).limited


def test_non_json_stdout_is_the_message_when_stderr_is_empty():
    res = parse_output("Claude usage limit reached\n", "", 1)
    assert res.limited and res.text == "Claude usage limit reached"


def test_plain_json_output_is_still_the_result():
    plain = parse_output('{"subtype":"success","is_error":false,"result":"hi","session_id":"p1",'
                         '"structured_output":{"a":1},"permission_denials":[{"tool_name":"Bash"}]}', "", 0)
    assert (plain.ok, plain.session_id, plain.text, plain.structured) == (True, "p1", "hi", {"a": 1})
    assert plain.denials == [{"tool_name": "Bash"}]
    typed = parse_output('{"type":"result","subtype":"success","is_error":false,"result":"hi",'
                         '"structured_output":{"a":1}}', "", 0)
    assert typed.ok
    assert parse_output("{}", "", 0).denials == []


def test_result_event_wins_over_later_events_and_borrows_the_session():
    out = "\n".join([
        '{"type":"system","subtype":"init","session_id":"s3"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"","structured_output":{"a":1}}',
        '{"type":"system","subtype":"trailer"}',
    ])
    res = parse_output(out, "", 0)
    assert (res.ok, res.session_id, res.subtype) == (True, "s3", "success")


LIMITS = [
    "You've hit your 5-hour limit · resets 11:30pm (Europe/Stockholm)",
    "You’ve hit your weekly limit · resets Nov 12",
    "Claude usage limit reached",
    "Claude AI usage limit reached|1793797800",
    "5-hour limit reached ∙ resets 3pm",
    "Weekly limit reached",
    "You have reached your usage limit",
    "rate limit exceeded",
    "Rate limit reached for requests",
    "You exceeded your rate limit",
    "rate_limit_error",
    '{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed..."}}',
    "overloaded_error",
    '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    "API Error: Repeated 529 Overloaded errors",
    "API Error (529)",
    "Error: 529 overloaded",
    "429 Too Many Requests: rate limit",
    "Request failed with status code 429",
    "HTTP 429",
    "You are now using low-priority capacity",
    "No capacity available for low priority requests",
    "Due to unexpected capacity constraints, Claude is unable to respond",
]

NOT_LIMITS = [
    "inventory capacity check failed",
    "the fire rate limit of the weapon",
    "fire rate limited to 5 shots per second",
    "stack limit reached for slot 3",
    "the player is overloaded and cannot move",
    "expected 429 gold, got 430",
    "AssertionError: 429 != 529",
    "the cooldown resets 3 seconds after the last hit",
    "level cap resets at 10",
    "low priority task: tidy the loot table",
    "bag is at capacity",
    "time limit exceeded in test_combat",
    "character hit the level limit",
    "test_rate_limiter.py::test_bucket failed",
    "",
]


@pytest.mark.parametrize("msg", LIMITS)
def test_limit_messages_match(msg):
    assert LIMIT_RE.search(msg), msg
    as_result = json.dumps({"subtype": "success", "is_error": True, "result": msg})
    assert parse_output(as_result, "", 1).limited
    assert parse_output("", msg, 1).limited


@pytest.mark.parametrize("msg", NOT_LIMITS)
def test_ordinary_prose_is_not_a_limit(msg):
    assert not LIMIT_RE.search(msg), msg
    as_result = json.dumps({"subtype": "success", "is_error": True, "result": msg})
    assert not parse_output(as_result, "", 1).limited


def test_successful_run_is_never_limited():
    out = '{"subtype":"success","is_error":false,"result":"rate limit exceeded","structured_output":{}}'
    assert not parse_output(out, "", 0).limited


def test_reset_time_still_parses_from_a_limit_message():
    import time
    noon = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1))
    msg = LIMITS[0]
    assert LIMIT_RE.search(msg)
    assert time.localtime(message_reset(msg, noon))[3:5] == (23, 30)
