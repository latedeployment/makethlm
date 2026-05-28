"""Shared fixtures for makethlm tests.

Session-scoped fixtures for SSH integration tests that spin up a Docker
container running sshd with key-based auth.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from makethlm.dispatcher import DryRunDispatcher
from makethlm.parser import parse
from makethlm.runner import Runner

# ---------------------------------------------------------------------------
# --no-docker flag: skip integration tests on machines without Docker
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--no-docker",
        action="store_true",
        default=False,
        help="Skip integration tests that require Docker",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--no-docker"):
        return
    skip = pytest.mark.skip(reason="--no-docker flag set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# SSH integration fixtures (session-scoped, require Docker)
# ---------------------------------------------------------------------------

_SSH_CONTAINER_DIR = Path(__file__).parent / "ssh_container"
_IMAGE_NAME = "makethlm-test-sshd"
_CONTAINER_NAME = "makethlm-test-sshd-run"


def _docker_available() -> bool:
    """Check whether Docker is available on this system."""
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def ssh_keypair(tmp_path_factory):
    """Generate an ephemeral RSA keypair for SSH integration tests.

    Returns (private_key_path, public_key_path).
    """
    key_dir = tmp_path_factory.mktemp("ssh_keys")
    private_key = key_dir / "id_rsa"
    public_key = key_dir / "id_rsa.pub"

    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(private_key)],
        capture_output=True,
        check=True,
    )
    # Ensure correct permissions
    os.chmod(private_key, 0o600)

    return (private_key, public_key)


@pytest.fixture(scope="session")
def ssh_container(ssh_keypair):
    """Build and run a Docker container with sshd for integration tests.

    Yields (host, port, user, private_key_path).
    Stops and removes the container on teardown.
    """
    if not _docker_available():
        pytest.fail("Docker is required for integration tests. "
                     "Use --no-docker to skip them.")

    private_key, public_key = ssh_keypair

    # Copy the public key into the build context as authorized_keys
    auth_keys_dst = _SSH_CONTAINER_DIR / "authorized_keys"
    shutil.copy(public_key, auth_keys_dst)

    try:
        # Build image
        subprocess.run(
            ["docker", "build", "-t", _IMAGE_NAME, str(_SSH_CONTAINER_DIR)],
            capture_output=True,
            check=True,
            timeout=120,
        )

        # Pick a free port
        port = _free_port()

        # Remove any leftover container with the same name
        subprocess.run(
            ["docker", "rm", "-f", _CONTAINER_NAME],
            capture_output=True,
            timeout=10,
        )

        # Start container
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", _CONTAINER_NAME,
                "-p", f"127.0.0.1:{port}:22",
                _IMAGE_NAME,
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )

        # Wait for sshd to be ready (poll with ssh)
        _wait_for_sshd("127.0.0.1", port, str(private_key), timeout=30)

        yield ("127.0.0.1", port, "testuser", private_key)

    finally:
        # Cleanup: stop and remove container
        subprocess.run(
            ["docker", "rm", "-f", _CONTAINER_NAME],
            capture_output=True,
            timeout=15,
        )
        # Remove the copied authorized_keys from build context
        if auth_keys_dst.exists():
            auth_keys_dst.unlink()


def _wait_for_sshd(host: str, port: int, key_path: str, timeout: int = 30) -> None:
    """Poll until sshd accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=2",
                    "-i", key_path,
                    "-p", str(port),
                    f"testuser@{host}",
                    "true",
                ],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise RuntimeError(f"sshd not ready on {host}:{port} after {timeout}s")


@pytest.fixture(scope="session")
def ssh_runner(ssh_container):
    """Helper that creates a Runner from a Promptfile string.

    The returned callable accepts a promptfile string and returns a Runner
    pre-configured with the container's host/port/user.

    Usage in tests::

        def test_foo(ssh_runner):
            runner, pf = ssh_runner(\"\"\"\\
                hosts target:
                    {host}

                task mytask [on=target]:
                    !echo hello
            \"\"\")
            result = runner.run("mytask")
    """
    host, port, user, key_path = ssh_container

    def _make_runner(promptfile_text: str) -> tuple[Runner, object]:
        # Substitute {host}, {port}, {user} placeholders
        text = (
            promptfile_text
            .replace("{host}", host)
            .replace("{port}", str(port))
            .replace("{user}", user)
        )
        pf = parse(text)

        # Inject port and user into all host groups
        for group in pf.host_groups.values():
            if group.port is None:
                group.port = port
            if group.user is None:
                group.user = user

        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher, verbose=False)
        return runner, pf

    return _make_runner, str(key_path)
