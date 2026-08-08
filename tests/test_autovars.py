"""Tests for make-style automatic variables available inside a task."""

from __future__ import annotations

import os

from makethlm.dispatcher import DryRunDispatcher
from makethlm.parser import parse
from makethlm.runner import Runner


def _touch(path, contents="x", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _resolve(pf, task="build"):
    """Return the resolved shell step contents for a task."""
    return [step.content for step in pf.resolve_steps(task) if step.kind == "shell"]


class TestTargetAndDependencies:
    def test_task_name(self):
        pf = parse("task build:\n    !echo {{makethlm_task}}\n")
        assert _resolve(pf) == ["echo build"]

    def test_all_dependencies(self):
        pf = parse(
            "task a:\n    !x\n\ntask b:\n    !x\n\ntask build: a b:\n    !echo {{makethlm_deps}}\n"
        )
        assert _resolve(pf) == ["echo a b"]

    def test_first_dependency(self):
        pf = parse(
            "task a:\n    !x\n\ntask b:\n    !x\n\ntask build: a b:\n    !echo {{makethlm_dep}}\n"
        )
        assert _resolve(pf) == ["echo a"]

    def test_empty_when_there_are_no_dependencies(self):
        pf = parse("task build:\n    !echo '{{makethlm_deps}}{{makethlm_dep}}'\n")
        assert _resolve(pf) == ["echo ''"]


class TestFileVariables:
    def _pf(self, tmp_path, body='task build [sources="src/*.c", outputs="build/app"]:'):
        return parse(
            f"{body}\n"
            "    !echo s={{makethlm_sources}}\n"
            "    !echo o={{makethlm_outputs}}\n"
            "    !echo c={{makethlm_changed}}\n"
        )

    def test_sources_expand_relative_to_the_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "src" / "a.c")
        _touch(tmp_path / "src" / "b.c")
        steps = _resolve(self._pf(tmp_path))
        assert steps[0] == "echo s=src/a.c src/b.c"

    def test_outputs_fall_back_to_the_declared_pattern(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "src" / "a.c")
        # build/app does not exist yet
        steps = _resolve(self._pf(tmp_path))
        assert steps[1] == "echo o=build/app"

    def test_changed_lists_only_sources_newer_than_the_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "src" / "old.c", mtime=1000)
        _touch(tmp_path / "src" / "new.c", mtime=3000)
        _touch(tmp_path / "build" / "app", mtime=2000)
        steps = _resolve(self._pf(tmp_path))
        assert steps[2] == "echo c=src/new.c"

    def test_changed_is_every_source_when_no_output_exists(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "src" / "a.c")
        steps = _resolve(self._pf(tmp_path))
        assert steps[2] == "echo c=src/a.c"

    def test_paths_with_spaces_are_quoted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "src" / "two words.c")
        steps = _resolve(self._pf(tmp_path))
        assert "'src/two words.c'" in steps[0]

    def test_empty_without_sources_or_outputs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pf = parse(
            "task build:\n"
            "    !echo 's={{makethlm_sources}}o={{makethlm_outputs}}c={{makethlm_changed}}'\n"
        )
        assert _resolve(pf) == ["echo 's=o=c='"]


class TestEndToEnd:
    def test_variables_reach_a_real_shell_command(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "src" / "a.c")
        pf = parse(
            'task build [sources="src/*.c", outputs="out.txt"]:\n'
            "    !echo {{makethlm_sources}} > out.txt\n"
        )
        result = Runner(pf, DryRunDispatcher(), verbose=False).run("build")
        assert result.success
        assert (tmp_path / "out.txt").read_text().strip() == "src/a.c"
