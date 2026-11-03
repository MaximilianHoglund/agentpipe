"""Subprocess helpers shared by the git, gh and claude wrappers."""

from __future__ import annotations

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
    try:
        p = subprocess.run(
            cmd, cwd=cwd, input=input, env=env, timeout=timeout, shell=shell,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        r = Result(p.returncode, p.stdout or "", p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        r = Result(TIMEOUT_CODE, out, f"timed out after {timeout}s")
    if check and not r.ok:
        raise CommandError(cmd, r)
    return r
