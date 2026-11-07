"""A throwaway git repo for tests: branch `base` and a `feature` branch off it."""

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_repo(root: Path) -> Path:
    """`lib.rs` (`mod a;`, `mod z;`) and `run.rs` (`x = 0`) on `base`; `feature` checked out."""
    r = root / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "base")
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("core.autocrlf", "false")):
        git(r, "config", k, v)
    (r / "lib.rs").write_bytes(b"mod a;\nmod z;\n")
    (r / "run.rs").write_bytes(b"x = 0\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    git(r, "checkout", "-qb", "feature")
    return r


def commit_on(r: Path, branch: str, path: str, text: str) -> None:
    git(r, "checkout", "-q", branch)
    (r / path).write_bytes(text.encode())
    git(r, "commit", "-qam", f"{branch} {path}")
