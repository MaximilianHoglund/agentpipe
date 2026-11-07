import asyncio
import json
import time

from agentpipe import panes, watch


def test_columns_grow_with_jobs():
    assert [panes.columns_for(n) for n in (0, 1, 2, 4, 5, 9, 10)] == [1, 1, 2, 2, 3, 3, 4]


def test_step_lines_use_the_theme_accent_not_ansi_magenta():
    from textual.app import App
    themes = App().available_themes
    s = panes.styles_for(themes["textual-dark"])
    assert s["step"] == "bold #FFA62B" or s["step"].lower() == "bold #ffa62b"
    assert "magenta" not in s.values() and "cyan" not in s.values()
    for name, theme in themes.items():
        assert all(v for k, v in panes.styles_for(theme).items() if k != "think"), name


def test_summarize_counts_tools_errors_and_denials():
    E = watch.Event
    group = [E("t", "tool", "Bash: a"), E("t", "tool", "Read: b"), E("t", "tool", "Bash: c"),
             E("t", "error", "boom"), E("t", "denied", "DENIED Bash: x")]
    assert panes.summarize(group) == "⋯ 3 tool calls (Bash 2, Read 1), 1 error, 1 DENIED"


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


def test_theme_is_remembered_and_ansi_themes_are_not_offered(tmp_path):
    work = tmp_path / "arpg"
    (work / "logs").mkdir(parents=True)

    async def go(expect):
        app = panes.PanesApp("arpg", work, ".", interval=60)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "ansi-dark" not in app.available_themes
            assert app.theme == expect
            app.theme = "nord"
            await pilot.pause()

    asyncio.run(go("textual-dark"))
    assert (tmp_path / "watch-theme").read_text(encoding="utf-8") == "nord"
    asyncio.run(go("nord"))


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
    think = "Checking whether validate covers UnitSpawn now that it is no longer Copy. " * 6
    msgs = [{"type": "assistant", "timestamp": "2026-11-07T10:00:00Z",
             "message": {"content": [{"type": "thinking", "thinking": think}]}},
            {"type": "assistant", "timestamp": "2026-11-07T10:00:01Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash",
                                      "input": {"command": "cargo test --workspace " + "x" * 300}}]}},
            {"type": "assistant", "timestamp": "2026-11-07T10:00:02Z",
             "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.rs"}}]}},
            {"type": "assistant", "timestamp": "2026-11-07T10:00:03Z",
             "message": {"content": [{"type": "text", "text": "Tests pass; writing the summary."}]}},
            {"type": "assistant", "timestamp": "2026-11-07T10:00:04Z",
             "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}}]
    (folder / "s.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs), encoding="utf-8")

    async def go():
        app = panes.PanesApp("arpg", work, ".", interval=60)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert set(app.panes) == {"c/1.1", "c/1.2"}
            impl, rev = app.panes["c/1.1"], app.panes["c/1.2"]
            text = "\n".join(s.text for s in impl.lines)
            assert "Checking whether validate" in text and "Tests pass" in text
            assert "2 tool calls (Bash 1, Read 1)" in text and "cargo test" not in text
            assert "3 tools" in str(impl.border_subtitle) and "now Bash: git status" in str(impl.border_subtitle)
            assert impl.virtual_size.width <= impl.scrollable_content_region.width  # wrapped, no h-scroll
            await pilot.press("t")
            await pilot.pause()
            assert "cargo test" in "\n".join(s.text for s in impl.lines)
            assert impl.virtual_size.width <= impl.scrollable_content_region.width  # long command wraps too
            await pilot.press("t")
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
