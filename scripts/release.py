#!/usr/bin/env python3
"""Prepare a makethlm release.

This script updates versions, moves changelog entries from Unreleased to the
new version, creates a git commit and tag, and optionally builds/publishes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "makethlm" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, check=check)


def current_version() -> str:
    text = PYPROJECT.read_text()
    match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find project version in pyproject.toml")
    return match.group(1)


def bump_version(version: str, bump: str) -> str:
    major, minor, patch = [int(part) for part in version.split(".")]
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return bump


def replace_version(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    text = text.replace(f'version = "{old}"', f'version = "{new}"')
    text = text.replace(f'__version__ = "{old}"', f'__version__ = "{new}"')
    path.write_text(text)


def update_changelog(version: str) -> None:
    text = CHANGELOG.read_text()
    heading = f"## {version} - {date.today().isoformat()}"
    text = text.replace(
        "## Unreleased\n", f"## Unreleased\n\n## {version} - {date.today().isoformat()}\n", 1
    )
    text = re.sub(rf"{re.escape(heading)}\n\n+", f"{heading}\n\n", text)
    CHANGELOG.write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a makethlm release")
    parser.add_argument("bump", help="major, minor, patch, or an explicit version")
    parser.add_argument("--no-tag", action="store_true", help="Do not create a git tag")
    parser.add_argument("--no-build", action="store_true", help="Do not build/validate the package")
    parser.add_argument("--publish", action="store_true", help="Publish after building")
    args = parser.parse_args()

    old = current_version()
    new = bump_version(old, args.bump)
    replace_version(PYPROJECT, old, new)
    replace_version(INIT, old, new)
    update_changelog(new)

    run(["uv", "run", "ruff", "check", "."])
    run(["uv", "run", "ruff", "format", "--check", "."])
    run(["uv", "run", "pytest", "tests/", "-q", "--no-docker"])
    if not args.no_build:
        publish_args = ["./publish.sh", "--skip-tests"]
        if not args.publish:
            publish_args.append("--validate")
        run(publish_args)

    run(["git", "add", "pyproject.toml", "makethlm/__init__.py", "CHANGELOG.md"])
    run(["git", "commit", "-m", f"Release {new}"])
    if not args.no_tag:
        run(["git", "tag", "-a", f"v{new}", "-m", f"Release {new}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
