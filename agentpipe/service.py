"""Run the dispatcher in the background: start it hidden, see whether it's alive, stop it.

``agentpipe start`` launches ``agentpipe run`` with no console window, its stdout and
stderr appended to ``logs/dispatcher.out``. The dispatcher writes ``dispatcher.pid``
while it runs. ``agentpipe stop`` creates a ``stop`` file: the dispatcher starts nothing
new, lets running agents finish, and exits. ``--now`` kills it and its agents instead
(work resumes on the next start, minus the killed agents' unsaved progress).

The watcher window ``start`` opens is linked: once the dispatcher has seen a linked
watcher, closing the last one stops it the same way as ``stop`` (Max closes the window
expecting the pipeline to stop). ``start --no-watch``, ``run`` and plain ``watch`` aren't
linked.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

_STILL_ACTIVE = 259


def is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT.
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def running_pid(work_dir: Path) -> int | None:
    try:
        pid = int((work_dir / "dispatcher.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if is_alive(pid) else None


def start(target: str, work_dir: Path, config: Path) -> int:
    """Launch a hidden dispatcher; returns its pid."""
    logs = work_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "dispatcher.out", "a", encoding="utf-8")
    cmd = [sys.executable, "-m", "agentpipe", "--config", str(config), "run", target]
    cwd = Path(__file__).resolve().parent.parent
    if os.name != "nt":
        return subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True).pid
    base = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # Break away from the launcher's job object (a terminal or tool runner may kill its
    # job on exit); not every job allows it, so fall back.
    for flags in (base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            return subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags).pid
        except OSError:
            continue
    raise RuntimeError("could not start the dispatcher")


def open_watcher(target: str, config: Path) -> int | None:
    """Open ``agentpipe watch`` in its own visible console window; returns its pid.

    Max wants every started pipeline to have a watcher attached. Off Windows there is no
    portable way to open a terminal, so this returns None and the caller prints the command.
    """
    if os.name != "nt":
        return None
    cmd = [sys.executable, "-m", "agentpipe", "--config", str(config), "watch", target, "--linked"]
    cwd = Path(__file__).resolve().parent.parent
    base = subprocess.CREATE_NEW_CONSOLE
    for flags in (base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            return subprocess.Popen(cmd, cwd=cwd, creationflags=flags).pid
        except OSError:
            continue
    return None


def watchers_dir(work_dir: Path) -> Path:
    return work_dir / "watchers"


def register_watcher(work_dir: Path) -> Path:
    """A linked watcher (the window ``start`` opens) records its pid here.

    The file stays behind when the window is closed; the dispatcher tells by the pid.
    """
    d = watchers_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / str(os.getpid())
    f.write_text("", encoding="utf-8")
    return f


def live_watchers(work_dir: Path) -> int:
    """Count live linked watchers, deleting the files of dead ones."""
    n = 0
    for f in watchers_dir(work_dir).glob("*"):
        try:
            alive = is_alive(int(f.name))
        except ValueError:
            alive = False
        if alive:
            n += 1
        else:
            f.unlink(missing_ok=True)
    return n


def request_stop(work_dir: Path) -> None:
    (work_dir / "stop").write_text("", encoding="utf-8")


def kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(os.getpgid(pid), 9)
