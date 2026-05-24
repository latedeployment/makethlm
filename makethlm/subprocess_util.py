"""Subprocess helper that kills children immediately on Ctrl-C."""

from __future__ import annotations

import os
import signal
import subprocess


def run_subprocess(
    cmd: str | list[str],
    *,
    shell: bool = False,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    cwd: str | None = None,
    executable: str | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess that dies immediately on Ctrl-C.

    Unlike subprocess.run(), this uses Popen with start_new_session=True
    so we can kill the entire child process group instantly when
    KeyboardInterrupt is raised, instead of blocking while the child
    handles SIGINT.
    """
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        cwd=cwd,
        executable=executable,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    except KeyboardInterrupt:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )
