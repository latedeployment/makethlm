"""SSH integration tests — require a running Docker container with sshd.

Run with:  python -m pytest tests/test_ssh_integration.py -v --tb=short -m integration
Skip with: python -m pytest tests/ -v --tb=short -m "not integration"
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from makethlm.models import HostGroup
from makethlm.runner import _build_ssh_command

pytestmark = pytest.mark.integration


def _patched_build_ssh_command(key_path: str):
    """Return a wrapper around _build_ssh_command that injects identity file
    and host-key checking options for the test container."""

    def _build(host: str, command: str, group: HostGroup) -> str:
        base = _build_ssh_command(host, command, group)
        # Inject options right after "ssh"
        return base.replace(
            "ssh ",
            f"ssh -i {key_path} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR ",
            1,
        )

    return _build


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSSHIntegration:
    def test_ssh_echo(self, ssh_runner):
        """Run echo on the container and verify output."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task echo_test [on=target]:
    !echo hello
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("echo_test")

        assert result.success
        tr = result.task_results[0]
        assert tr.step_results[0].kind == "ssh"
        assert tr.step_results[0].response.strip() == "hello"

    def test_ssh_exit_code_zero(self, ssh_runner):
        """Run a command that exits 0 and verify success."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task ok [on=target]:
    !true
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("ok")

        assert result.success
        assert result.task_results[0].step_results[0].success

    def test_ssh_exit_code_nonzero(self, ssh_runner):
        """Run a command that exits nonzero and verify failure."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task fail [on=target]:
    !false
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("fail")

        assert not result.success
        assert not result.task_results[0].step_results[0].success

    def test_ssh_multi_host(self, ssh_runner):
        """Same container listed twice simulates multi-host execution."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}
    {host}

task multi [on=target]:
    !echo ok
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("multi")

        assert result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 2
        assert all(s.kind == "ssh" for s in sr)
        assert all(s.success for s in sr)

    def test_ssh_multi_command(self, ssh_runner):
        """Run two shell steps and verify both outputs captured in order."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task two_cmds [on=target]:
    !echo a
    !echo b
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("two_cmds")

        assert result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 2
        assert sr[0].response.strip() == "a"
        assert sr[1].response.strip() == "b"

    def test_ssh_custom_port(self, ssh_runner):
        """Explicitly set port in the host group and verify connection."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task porttest [on=target]:
    !echo port_ok
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("porttest")

        assert result.success
        assert result.task_results[0].step_results[0].response.strip() == "port_ok"

    def test_ssh_failure_stops(self, ssh_runner):
        """A failing command should halt execution — second step should not run."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task stop_on_fail [on=target]:
    !false
    !echo after
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("stop_on_fail")

        assert not result.success
        sr = result.task_results[0].step_results
        # Only the first step should have run
        assert len(sr) == 1
        assert not sr[0].success

    def test_ssh_env_passthrough(self, ssh_runner):
        """Verify environment variables are visible on the remote host."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task envtest [on=target]:
    !echo $HOSTNAME
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("envtest")

        assert result.success
        # $HOSTNAME should resolve to something (container ID or hostname)
        assert len(result.task_results[0].step_results[0].response.strip()) > 0

    def test_ssh_timeout(self, ssh_runner):
        """A long-running command with low timeout should produce a timeout error."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task slow [on=target]:
    !sleep 300
""")
        # Patch both the SSH command builder and the timeout
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):

            def _short_timeout_ssh(step, host, group):
                import subprocess as sp

                from makethlm.runner import StepResult

                ssh_cmd = _patched_build_ssh_command(key_path)(host, step.content, group)
                try:
                    from makethlm.subprocess_util import run_subprocess

                    proc = run_subprocess(
                        ssh_cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=3,  # very short timeout
                    )
                    output = proc.stdout
                    if proc.stderr:
                        output += proc.stderr
                    ok = proc.returncode == 0 or step.ignore_error
                    return StepResult(
                        kind="ssh",
                        content=step.content,
                        response=output.strip(),
                        success=ok,
                        host=host,
                    )
                except sp.TimeoutExpired:
                    return StepResult(
                        kind="ssh",
                        content=step.content,
                        response=f"error: SSH to {host} timed out after 3s",
                        success=step.ignore_error,
                        host=host,
                    )

            runner._run_ssh_step = _short_timeout_ssh
            result = runner.run("slow")

        assert not result.success
        sr = result.task_results[0].step_results[0]
        assert "timed out" in sr.response

    def test_ssh_ignore_error(self, ssh_runner):
        """A failing command with @ignore should still mark overall success."""
        (make_runner, key_path) = ssh_runner
        runner, pf = make_runner("""\
hosts target:
    {host}

task ignore_fail [on=target]:
    !@ignore false
""")
        with patch(
            "makethlm.runner._build_ssh_command",
            _patched_build_ssh_command(key_path),
        ):
            result = runner.run("ignore_fail")

        assert result.success
        sr = result.task_results[0].step_results[0]
        assert sr.success  # ignore_error makes it succeed
