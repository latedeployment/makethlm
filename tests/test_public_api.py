"""Tests for public package imports and golden Promptfiles."""

from pathlib import Path

import makethlm


def test_public_api_exports_core_objects():
    assert makethlm.parse is not None
    assert makethlm.Runner is not None
    assert makethlm.DryRunDispatcher is not None
    assert makethlm.CodexDispatcher is not None


def test_golden_devops_promptfile_parses():
    source = Path("tests/golden/devops.pf").read_text()
    pf = makethlm.parse(source, filename="tests/golden/devops.pf")

    assert pf.default_task == "build"
    assert pf.host_groups["web"].identity_file == "~/.ssh/deploy"
    assert pf.tasks["build"].options.timeout == "30s"
    assert pf.tasks["deploy"].dependencies == ["build"]
    assert pf.tasks["deploy"].options.ssh_parallel is True
    assert pf.tasks["deploy"].options.rollback == "rollback"
