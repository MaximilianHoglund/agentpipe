"""Subprocess helpers shared by the git, gh and claude wrappers."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


TIMEOUT_CODE = 124


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, chars: int = 6000) -> str:
        text = (self.out + ("\n" + self.err if self.err else "")).strip()
        return text if len(text) <= chars else "…\n" + text[-chars:]


class CommandError(RuntimeError):
    def __init__(self, cmd, result: Result):
        super().__init__(f"{' '.join(map(str, cmd))} exited {result.code}: {result.tail(2000)}")
        self.result = result


def run(cmd, cwd: Path | None = None, *, input: str | None = None, env=None,
        timeout: int | None = None, check: bool = True, shell: bool = False) -> Result:
    # Not subprocess.run: on timeout it kills only the direct child (the shell, with shell=True)
    # and then waits for the output pipes, which grandchildren such as cargo-mutants hold open,
    # so the timeout never took effect (a 30 min mutation cap ran past 49 min). Kill the tree.
    p = subprocess.Popen(
        cmd, cwd=cwd, env=env, shell=shell,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        start_new_session=os.name != "nt",
    )
    try:
        out, err = p.communicate(input, timeout=timeout)
        r = Result(p.returncode, out or "", err or "")
    except subprocess.TimeoutExpired:
        kill_tree(p)
        out, _ = p.communicate()
        r = Result(TIMEOUT_CODE, out or "", f"timed out after {timeout}s")
    if check and not r.ok:
        raise CommandError(cmd, r)
    return r


def kill_tree(p: subprocess.Popen) -> None:
    """Kill a process and everything it started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    p.kill()
