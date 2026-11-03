import json
import os
import time

from agentpipe import watch

LOG = """\
2026-11-03 12:00:00 [t] running; implementers=2 reviewers=1
2026-11-03 12:00:01 [t] start implement c/1.1 (#5) on impl-0
2026-11-03 12:00:02 [t] start implement c/1.2 (#6) on impl-1
2026-11-03 12:10:00 [t] done implement c/1.1
2026-11-03 12:11:00 [t] start review c/1.1 (#5) on review-0
2026-11-03 12:12:00 [t] implement c/1.2 crashed: boom
2026-11-03 12:13:00 [t] start implement c/1.3 (#7) on impl-1
"""


def test_parse_log_tracks_running_jobs_and_resets_on_restart():
    st = watch.parse_log(LOG.splitlines())
    assert [(j.kind, j.key, j.issue, j.slot) for j in st.jobs] == [
        ("review", "c/1.1", 5, "review-0"), ("implement", "c/1.3", 7, "impl-1")]
    assert st.recent[-1] == "12:13:00 start implement c/1.3 (#7) on impl-1"
    restarted = watch.parse_log((LOG + "2026-11-03 12:20:00 [t] running; implementers=2\n").splitlines())
    assert restarted.jobs == []


def test_project_folder_matches_claude_code_naming():
    assert watch.project_folder(r"E:\Source\.pipeline\arpg\base").name == "E--Source--pipeline-arpg-base"


def test_job_cwd_uses_worktree_or_review_checkout():
    job = watch.Job("implement", "c/1.2", 1, "impl-0", 0)
    assert watch.job_cwd(watch.Path("W"), ".", job) == watch.Path("W/wt/c-1_2")
    rv = watch.Job("review", "c/1.2", 1, "review-0", 0)
    assert watch.job_cwd(watch.Path("W"), "dota", rv) == watch.Path("W/review/c-1_2/dota")


def _line(kind, content, ts="2026-11-03T12:00:00.000Z"):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content}})


def test_parse_transcript_summarises_tools_text_errors_and_ignores_partial_lines():
    lines = [
        json.dumps({"type": "queue-operation"}),
        _line("assistant", [{"type": "text", "text": "Reading the spec."},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "cargo test"}}]),
        _line("user", [{"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "failed"}]}]),
        _line("assistant", [{"type": "tool_use", "name": "StructuredOutput", "input": {}}]),
        '{"type": "assistant", "mess',
    ]
    tr = watch.parse_transcript(lines)
    assert [(e.kind, e.text) for e in tr.events] == [
        ("text", "Reading the spec."), ("tool", "Bash: cargo test"), ("error", "failed"),
        ("done", "final answer submitted")]
    assert (tr.tool_calls, tr.errors) == (2, 1)


def test_render_shows_live_agent_and_link_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "PROJECTS", tmp_path / "projects")
    work = tmp_path / "work"
    (work / "logs").mkdir(parents=True)
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
    (work / "logs" / "pipeline.log").write_text(
        f"{stamp} [t] running; x\n{stamp} [t] start implement c/1.1 (#5) on impl-0\n", encoding="utf-8")
    (work / "state.json").write_text(json.dumps({"links": {"c/1.1": []}, "paused_until": 0}), encoding="utf-8")
    (work / "plan.json").write_text(json.dumps({"tasks": {"c/1.1": {}, "c/1.2": {}}}), encoding="utf-8")
    folder = watch.project_folder(work / "wt" / "c-1_1")
    folder.mkdir(parents=True)
    tr = folder / "s.jsonl"
    tr.write_text(_line("assistant", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.rs"}}]) + "\n",
                  encoding="utf-8")
    os.utime(tr, (now, now))
    text = watch.render("t", work, ".", color=False, now=now)
    assert "blocked-by links synced for 1 of 2 tasks" in text
    assert "[impl-0] implement c/1.1 (#5), 60s: agent, 1 tool calls, 0 errors" in text
    assert "last activity 0s ago" in text and "STALLED" not in text
    assert "Edit: src/a.rs" in text


def test_steps_without_an_agent_show_progress_and_flag_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "PROJECTS", tmp_path / "projects")
    work = tmp_path / "work"
    (work / "logs").mkdir(parents=True)
    now = time.time()
    t0 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 1200))
    (work / "logs" / "pipeline.log").write_text(
        f"{t0} [t] running (pid 1); x\n{t0} [t] start review c/1.1 (#5) on review-0\n"
        f"{t0} [t] phase c/1.1: test gate\n{t0} [t] start implement c/1.2 (#6) on impl-0\n", encoding="utf-8")
    mo = work / "review" / "c-1_1" / "mutants.out"
    mo.mkdir(parents=True)
    (mo / "caught.txt").write_text("a\nb\n")
    (mo / "missed.txt").write_text("c\n")
    (mo / "outcomes.json").write_text(json.dumps({"total_mutants": 5}))
    text = watch.render("t", work, ".", color=False, now=now)
    assert "review c/1.1 (#5), 20m: mutation testing 3/5 mutants, 1 missed" in text
    # The implement job logged nothing since its start 20 minutes ago.
    assert "implement c/1.2 (#6), 20m: setting up (worktree, prompt)" in text
    assert "POSSIBLY STALLED" in text
    st = watch.parse_log((work / "logs" / "pipeline.log").read_text().splitlines())
    assert {j.key: j.phase for j in st.jobs}["c/1.1"] == "test gate"
