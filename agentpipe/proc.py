"""Subprocess helpers shared by the git, gh and claude wrappers."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
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
        """The end of the output, stdout last. Cut from one string with stderr appended, a long
        stderr (cargo's build lines) filled the whole tail and dropped the verdict, which
        scripts print on stdout: arpg's step-1 release failure (#722) named no failing test."""
        out, err = self.out.strip(), self.err.strip()
        if len(out) + len(err) + 1 > chars and err:
            keep = max(chars // 4, chars - len(out))  # stderr gets a quarter, or what stdout leaves
            err = err if len(err) <= keep else "…\n" + err[-keep:]
        text = (err + "\n" + out).strip() if err else out
        return text if len(text) <= chars else "…\n" + text[-chars:]


class CommandError(RuntimeError):
    def __init__(self, cmd, result: Result):
        super().__init__(f"{' '.join(map(str, cmd))} exited {result.code}: {result.tail(2000)}")
        self.result = result


def resolve(cmd, env, shell: bool):
    """Find an argument list's program on `env`'s PATH. Windows' CreateProcess searches the
    dispatcher's own PATH instead, so a tool only the target's env adds (cargo) wasn't found."""
    if shell or not env or isinstance(cmd, (str, bytes)) or not cmd:
        return cmd
    found = shutil.which(str(cmd[0]), path=env.get("PATH", env.get("Path")))
    return [found, *cmd[1:]] if found else cmd


def run(cmd, cwd: Path | None = None, *, input: str | None = None, env=None,
        timeout: int | None = None, check: bool = True, shell: bool = False) -> Result:
    # Not subprocess.run: on timeout it kills only the direct child (the shell, with shell=True)
    # and then waits for the output pipes, which grandchildren such as cargo-mutants hold open,
    # so the timeout never took effect (a 30 min mutation cap ran past 49 min). Kill the tree.
    p = subprocess.Popen(
        resolve(cmd, env, shell), cwd=cwd, env=env, shell=shell,
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


class Background:
    """A command started now and collected later, with `run`'s timeout and tree kill."""

    def __init__(self, cmd, cwd: Path | None = None, *, env=None, timeout: int | None = None,
                 shell: bool = False):
        self.p = subprocess.Popen(
            resolve(cmd, env, shell), cwd=cwd, env=env, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
        self._result: Result | None = None
        self._thread = threading.Thread(target=self._collect, args=(timeout,), daemon=True)
        self._thread.start()

    def _collect(self, timeout: int | None) -> None:
        try:
            out, err = self.p.communicate(timeout=timeout)
            self._result = Result(self.p.returncode, out or "", err or "")
        except subprocess.TimeoutExpired:
            kill_tree(self.p)
            out, _ = self.p.communicate()
            self._result = Result(TIMEOUT_CODE, out or "", f"timed out after {timeout}s")

    def result(self) -> Result:
        """Waits for the command to finish (or time out) and returns its result."""
        self._thread.join()
        assert self._result is not None
        return self._result

    def kill(self) -> None:
        """Stops the command and everything it started, and waits for that."""
        if self.p.poll() is None:
            kill_tree(self.p)
        self._thread.join()


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
