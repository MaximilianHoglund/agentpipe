import pytest

from agentpipe.tasks import TaskParseError, fingerprint, mark_done, parse_tasks

SAMPLE = """## 1. Setup

- [x] 1.1 Create workspace
- [ ] 1.2 Fetch fixtures
> a blockquote with - [ ] 9.9 inside is not a task
- [ ] unnumbered checkbox is ignored
  - [ ] 1.3 indented subtask

## 2. More

- [X] 2.4a Upper-case X counts as done
- [ ] 2.10 Ten
"""


def test_parses_numbered_tasks_with_sections_and_done_state():
    tasks = parse_tasks("c", SAMPLE)
    assert [(t.id, t.done, t.section) for t in tasks] == [
        ("1.1", True, "1. Setup"),
        ("1.2", False, "1. Setup"),
        ("1.3", False, "1. Setup"),
        ("2.4a", True, "2. More"),
        ("2.10", False, "2. More"),
    ]
    assert tasks[1].key == "c/1.2"
    assert tasks[1].text == "Fetch fixtures"


def test_empty_file_has_no_tasks():
    assert parse_tasks("c", "") == []


def test_duplicate_ids_are_rejected_with_both_lines():
    with pytest.raises(TaskParseError, match="lines 1 and 2"):
        parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.1 b\n")


def test_mark_done_ticks_only_the_named_task_and_keeps_line_endings():
    text = "- [ ] 1.1 a\r\n- [ ] 1.10 b\r\n"
    out = mark_done(text, "1.1")
    assert out == "- [x] 1.1 a\r\n- [ ] 1.10 b\r\n"
    assert mark_done(out, "1.1") == out  # idempotent


def test_mark_done_unknown_task_raises():
    with pytest.raises(TaskParseError):
        mark_done("- [ ] 1.1 a\n", "1.2")


def test_fingerprint_ignores_ticks_but_not_wording_or_new_tasks():
    a = parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.2 b\n")
    ticked = parse_tasks("c", "- [x] 1.1 a\n- [ ] 1.2 b\n")
    reworded = parse_tasks("c", "- [ ] 1.1 a!\n- [ ] 1.2 b\n")
    added = parse_tasks("c", "- [ ] 1.1 a\n- [ ] 1.2 b\n- [ ] 1.3 c\n")
    assert fingerprint(a) == fingerprint(ticked)
    assert fingerprint(a) != fingerprint(reworded)
    assert fingerprint(a) != fingerprint(added)
