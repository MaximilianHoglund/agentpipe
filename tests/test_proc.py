import os
import subprocess
import sys
import time

from agentpipe import proc


def test_timeout_kills_grandchildren_that_hold_the_output_open(tmp_path):
    # A shell whose grandchild keeps stdout open: subprocess.run's timeout waited for it.
    script = tmp_path / "spawn.py"
    script.write_text("import subprocess, sys\n"
                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']).wait()\n")
    start = time.monotonic()
    r = proc.run(f'"{sys.executable}" "{script}"', timeout=2, check=False, shell=True)
    assert r.code == proc.TIMEOUT_CODE
    assert time.monotonic() - start < 20


def test_run_returns_output_and_passes_input():
    r = proc.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="hi")
    assert r.ok and r.out.strip() == "HI"


def test_a_program_only_the_env_path_has_is_found(tmp_path):
    # Windows' CreateProcess searched the dispatcher's PATH, not env's: cargo, which only the
    # target's env adds, failed with WinError 2.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "envonly.bat").write_text("@echo found\n")
    else:
        prog = bin_dir / "envonly"
        prog.write_text("#!/bin/sh\necho found\n")
        prog.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir)}
    assert proc.run(["envonly"], env=env).out.strip() == "found"
