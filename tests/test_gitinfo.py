"""Tests for git-aware Promptfile inputs."""

from __future__ import annotations

import subprocess

import pytest

from makethlm import gitinfo
from makethlm.parser import parse


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A small git repository with one commit, as the working directory."""
    _git(["init", "-b", "main"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)\n")
    (tmp_path / "README.md").write_text("hello\n")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(gitinfo.SINCE_ENV_VAR, raising=False)
    return tmp_path


class TestChangedFiles:
    def test_clean_tree_has_no_changes(self, repo):
        assert gitinfo.changed_files() == []

    def test_modified_file_is_listed(self, repo):
        (repo / "src" / "app.py").write_text("print(2)\n")
        assert gitinfo.changed_files() == ["src/app.py"]

    def test_untracked_file_is_listed(self, repo):
        (repo / "new.txt").write_text("x\n")
        assert "new.txt" in gitinfo.changed_files()

    def test_ignored_file_is_not_listed(self, repo):
        (repo / ".gitignore").write_text("ignored.txt\n")
        (repo / "ignored.txt").write_text("x\n")
        assert "ignored.txt" not in gitinfo.changed_files()

    def test_outside_a_repository_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert gitinfo.changed_files() == []

    def test_explicit_ref_is_used(self, repo):
        (repo / "src" / "app.py").write_text("print(2)\n")
        _git(["commit", "-am", "second"], repo)
        assert gitinfo.changed_files("HEAD") == []
        assert gitinfo.changed_files("HEAD~1") == ["src/app.py"]

    def test_since_env_var_sets_the_default(self, repo, monkeypatch):
        (repo / "src" / "app.py").write_text("print(2)\n")
        _git(["commit", "-am", "second"], repo)
        monkeypatch.setenv(gitinfo.SINCE_ENV_VAR, "HEAD~1")
        assert gitinfo.changed_files() == ["src/app.py"]


class TestMatches:
    def test_glob_match(self):
        assert gitinfo.matches(["src/app.py"], "src/*.py")

    def test_recursive_prefix_match(self):
        assert gitinfo.matches(["src/deep/nested/app.py"], "src/**")

    def test_no_match(self):
        assert not gitinfo.matches(["docs/readme.md"], "src/**")

    def test_empty_paths_never_match(self):
        assert not gitinfo.matches([], "src/**")


class TestBranchAndSha:
    def test_branch(self, repo):
        assert gitinfo.branch() == "main"

    def test_sha_is_short_by_default(self, repo):
        short = gitinfo.sha()
        assert 6 <= len(short) <= 12
        assert gitinfo.sha(short=False).startswith(short)

    def test_empty_outside_a_repository(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert gitinfo.branch() == ""
        assert gitinfo.sha() == ""


class TestPromptfileFunctions:
    def test_changed_in_a_prompt(self, repo):
        (repo / "src" / "app.py").write_text("print(2)\n")
        pf = parse("""\
task review:
    changed: {{changed("src/**")}}
""")
        assert pf.resolve_prompt("review").strip() == "changed: true"

    def test_changed_with_explicit_ref(self, repo):
        (repo / "src" / "app.py").write_text("print(2)\n")
        _git(["commit", "-am", "second"], repo)
        pf = parse("""\
task review:
    changed: {{changed("HEAD~1", "src/**")}}
""")
        assert pf.resolve_prompt("review").strip() == "changed: true"

    def test_changed_files_in_a_prompt(self, repo):
        (repo / "README.md").write_text("goodbye\n")
        pf = parse("""\
task review:
    files: {{changed_files()}}
""")
        assert "README.md" in pf.resolve_prompt("review")

    def test_git_branch_and_sha_in_a_prompt(self, repo):
        pf = parse("""\
task release:
    releasing {{git_branch()}} at {{git_sha()}}
""")
        rendered = pf.resolve_prompt("release")
        assert "releasing main at" in rendered

    def test_changed_gates_a_when_condition(self, repo):
        # A task can skip itself when nothing in its area changed.
        pf = parse("""\
task review [when=changed("src/**") == "true"]:
    review the source changes
""")
        from makethlm.dispatcher import DryRunDispatcher
        from makethlm.runner import Runner

        result = Runner(pf, DryRunDispatcher(), verbose=False).run("review")
        assert "[skipped] when condition not met" in result.task_results[0].response
