"""Hand a running dispatcher's work over to a new one without idling slots.

When new agentpipe code lands, the running dispatcher starts the new one at once and keeps
only the agent jobs it already has, finishing them on its old code. Meanwhile it lists those
jobs in ``handoff/<its pid>.json``. The new dispatcher treats every task and slot listed by a
live old dispatcher as taken, so it fills only the free slots and starts none of that work
twice. Each old dispatcher deletes its file when its last job ends; a dead one's file is
ignored and removed.

Both dispatchers write ``state.json`` meanwhile; ``State`` merges their writes under
``state.lock`` (see ``file_lock``).
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from agentpipe import service


def handoff_dir(work_dir: Path) -> Path:
    return work_dir / "handoff"


def write(work_dir: Path, pid: int, jobs: dict[str, dict]) -> None:
    """Record the jobs dispatcher ``pid`` still runs: {key: {kind, slot, issue, batch}}."""
    d = handoff_dir(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{pid}.tmp"
    tmp.write_text(json.dumps({"pid": pid, "jobs": jobs}, indent=2), encoding="utf-8")
    from agentpipe.pipeline import replace_retrying
    replace_retrying(tmp, d / f"{pid}.json")


def remove(work_dir: Path, pid: int) -> None:
    (handoff_dir(work_dir) / f"{pid}.json").unlink(missing_ok=True)


def live(work_dir: Path, own_pid: int | None = None) -> dict[int, dict[str, dict]]:
    """Jobs of each live old dispatcher, by pid. Files of dead ones are deleted."""
    out: dict[int, dict[str, dict]] = {}
    d = handoff_dir(work_dir)
    for f in d.glob("*.json") if d.is_dir() else ():
        try:
            pid = int(f.stem)
        except ValueError:
            continue
        if pid == own_pid:
            continue
        if not service.is_alive(pid):
            f.unlink(missing_ok=True)
            continue
        try:
            out[pid] = json.loads(f.read_text(encoding="utf-8")).get("jobs") or {}
        except (OSError, ValueError):
            continue  # mid-write; read again next tick
    return out


class Busy(RuntimeError):
    """Another live process holds the lock."""


@contextmanager
def held_lock(path: Path):
    """An OS lock on ``path`` for as long as the block runs, taken without waiting.

    Raises ``Busy`` if another process holds it. The operating system frees it when the holder
    dies, so a crashed run never blocks it for good (unlike ``file_lock``'s stale timer, which
    can't tell a long run from a dead one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise Busy(str(path)) from e
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


@contextmanager
def file_lock(path: Path, stale: float = 30.0):
    """A lock shared between processes: the file exists while it is held.

    A file older than ``stale`` seconds belongs to a process that died holding it."""
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
