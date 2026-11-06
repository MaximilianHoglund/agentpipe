import asyncio
import json
import time

from agentpipe import panes, watch


def test_columns_grow_with_jobs():
    assert [panes.columns_for(n) for n in (0, 1, 2, 4, 5, 9, 10)] == [1, 1, 2, 2, 3, 3, 4]


def test_tail_returns_only_new_complete_lines(tmp_path):
    f = tmp_path / "x.log"
    f.write_bytes(b"a\nb\npart")
    t = panes.Tail(f)
    assert t.read() == ["a", "b"]
    with f.open("ab") as h:
        h.write(b"ial\nc\n")
    assert t.read() == ["partial", "c"]
    assert t.read() == []
    f.write_bytes(b"new\n")  # replaced by a shorter file: start over
    assert t.read() == ["new"]


def _stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def test_app_shows_a_pane_per_job_with_its_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "PROJECTS", tmp_path / "projects")
    work = tmp_path / "work"
    (work / "logs").mkdir(parents=True)
    now = time.time()
    (work / "logs" / "pipeline.log").write_text(
        f"{_stamp(now - 60)} [arpg] start implement c/1.1 (#5) on impl-0: add a thing\n"
        f"{_stamp(now - 50)} [arpg] start review c/1.2 (#6) on review-0\n"
        f"{_stamp(now - 40)} [arpg] phase c/1.2: test gate\n", encoding="utf-8")
    cwd = watch.job_cwd(work, ".", watch.Job("implement", "c/1.1", 5, "impl-0", now - 60))
    folder = watch.project_folder(cwd)
    folder.mkdir(parents=True)
    msg = {"type": "assistant", "timestamp": "2026-11-07T10:00:00Z",
           "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "cargo test"}}]}}
    (folder / "s.jsonl").write_text(json.dumps(msg) + "\n", encoding="utf-8")

    async def go():
        app = panes.PanesApp("arpg", work, ".", interval=60)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert set(app.panes) == {"c/1.1", "c/1.2"}
            impl, rev = app.panes["c/1.1"], app.panes["c/1.2"]
            assert "1 tools" in str(impl.border_subtitle)
            assert any("cargo test" in s.text for s in impl.lines)
            assert "test gate" in str(rev.border_subtitle)
            # A finished job's pane goes away on the next refresh.
            with (work / "logs" / "pipeline.log").open("a", encoding="utf-8") as f:
                f.write(f"{_stamp(now)} [arpg] done review c/1.2\n")
            app.tick()
            await pilot.pause()
            assert set(app.panes) == {"c/1.1"}
            app.set_focus(impl)
            await pilot.press("m")
            assert app.screen.maximized is impl
            await pilot.press("escape")
            assert app.screen.maximized is None

    asyncio.run(go())
