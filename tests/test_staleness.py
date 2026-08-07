"""Tests for file-based staleness (sources/outputs) skipping."""

from __future__ import annotations

import os
import threading

import pytest

from makethlm.dispatcher import DryRunDispatcher
from makethlm.parser import ParseError, parse
from makethlm.runner import Runner
from makethlm.staleness import (
    digest_sources,
    expand_patterns,
    split_patterns,
    up_to_date_reason,
)


def _touch(path, contents="x", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# Pattern helpers
# ---------------------------------------------------------------------------


class TestSplitPatterns:
    def test_comma_separated(self):
        assert split_patterns("src/*.c, include/*.h") == ["src/*.c", "include/*.h"]

    def test_pipe_separated(self):
        assert split_patterns("a.txt|b.txt") == ["a.txt", "b.txt"]

    def test_keeps_spaces_inside_paths(self):
        assert split_patterns("my dir/*.c") == ["my dir/*.c"]

    def test_ignores_empty_items(self):
        assert split_patterns("a.txt,,  ,b.txt") == ["a.txt", "b.txt"]


class TestExpandPatterns:
    def test_matches_files_only(self, tmp_path):
        _touch(tmp_path / "a.c")
        (tmp_path / "sub").mkdir()
        matched = expand_patterns(["*"], str(tmp_path))
        assert [p.name for p in matched] == ["a.c"]

    def test_recursive_glob(self, tmp_path):
        _touch(tmp_path / "src" / "deep" / "a.c")
        _touch(tmp_path / "src" / "b.c")
        matched = expand_patterns(["src/**/*.c"], str(tmp_path))
        assert sorted(p.name for p in matched) == ["a.c", "b.c"]

    def test_deduplicates_overlapping_patterns(self, tmp_path):
        _touch(tmp_path / "a.c")
        matched = expand_patterns(["*.c", "a.c"], str(tmp_path))
        assert len(matched) == 1


class TestDigestSources:
    def test_none_without_patterns(self, tmp_path):
        assert digest_sources([], str(tmp_path)) is None

    def test_changes_with_content(self, tmp_path):
        src = _touch(tmp_path / "a.c", "one")
        first = digest_sources(["*.c"], str(tmp_path))
        src.write_text("two")
        assert digest_sources(["*.c"], str(tmp_path)) != first

    def test_stable_for_identical_content(self, tmp_path):
        _touch(tmp_path / "a.c", "one")
        assert digest_sources(["*.c"], str(tmp_path)) == digest_sources(["*.c"], str(tmp_path))

    def test_changes_when_file_added(self, tmp_path):
        _touch(tmp_path / "a.c", "one")
        first = digest_sources(["*.c"], str(tmp_path))
        _touch(tmp_path / "b.c", "two")
        assert digest_sources(["*.c"], str(tmp_path)) != first


# ---------------------------------------------------------------------------
# Staleness decisions
# ---------------------------------------------------------------------------


class TestUpToDateReason:
    def test_requires_both_patterns(self, tmp_path):
        _touch(tmp_path / "a.c")
        assert up_to_date_reason(["*.c"], [], str(tmp_path)) is None
        assert up_to_date_reason([], ["*.o"], str(tmp_path)) is None

    def test_up_to_date_when_output_newer(self, tmp_path):
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        reason = up_to_date_reason(["a.c"], ["a.out"], str(tmp_path))
        assert reason is not None
        assert "up to date" in reason

    def test_stale_when_source_newer(self, tmp_path):
        _touch(tmp_path / "a.c", mtime=3000)
        _touch(tmp_path / "a.out", mtime=2000)
        assert up_to_date_reason(["a.c"], ["a.out"], str(tmp_path)) is None

    def test_equal_mtime_is_up_to_date(self, tmp_path):
        _touch(tmp_path / "a.c", mtime=2000)
        _touch(tmp_path / "a.out", mtime=2000)
        assert up_to_date_reason(["a.c"], ["a.out"], str(tmp_path)) is not None

    def test_stale_when_output_missing(self, tmp_path):
        _touch(tmp_path / "a.c", mtime=1000)
        assert up_to_date_reason(["a.c"], ["a.out"], str(tmp_path)) is None

    def test_stale_when_one_of_several_outputs_missing(self, tmp_path):
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        assert up_to_date_reason(["a.c"], ["a.out", "b.out"], str(tmp_path)) is None

    def test_stale_when_no_source_matches(self, tmp_path):
        _touch(tmp_path / "a.out", mtime=2000)
        assert up_to_date_reason(["*.c"], ["a.out"], str(tmp_path)) is None

    def test_oldest_output_decides(self, tmp_path):
        _touch(tmp_path / "a.c", mtime=2000)
        _touch(tmp_path / "new.out", mtime=3000)
        _touch(tmp_path / "old.out", mtime=1000)
        assert up_to_date_reason(["a.c"], ["*.out"], str(tmp_path)) is None

    def test_newest_source_decides(self, tmp_path):
        _touch(tmp_path / "old.c", mtime=1000)
        _touch(tmp_path / "new.c", mtime=3000)
        _touch(tmp_path / "a.out", mtime=2000)
        assert up_to_date_reason(["*.c"], ["a.out"], str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseSourcesOutputs:
    def test_parses_patterns(self):
        pf = parse("""\
task build [sources="src/*.c, include/*.h", outputs="build/app"]:
    !make
""")
        opts = pf.tasks["build"].options
        assert opts.sources == ["src/*.c", "include/*.h"]
        assert opts.outputs == ["build/app"]

    def test_singular_aliases(self):
        pf = parse("""\
task build [source="a.c", output="app"]:
    !make
""")
        assert pf.tasks["build"].options.sources == ["a.c"]
        assert pf.tasks["build"].options.outputs == ["app"]

    def test_empty_value_rejected(self):
        with pytest.raises(ParseError, match="sources requires at least one"):
            parse("""\
task build [sources=""]:
    !make
""")


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


def _runner(pf_text, tmp_path, **kwargs):
    pf = parse(pf_text)
    return Runner(pf, DryRunDispatcher(), verbose=False, **kwargs)


class TestRunnerStaleness:
    def test_skips_when_up_to_date(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch marker
""",
            tmp_path,
        )
        result = runner.run("build")
        assert result.success
        assert "[skipped] up to date" in result.task_results[0].response
        assert not (tmp_path / "marker").exists()

    def test_runs_when_source_is_newer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.out", mtime=1000)
        _touch(tmp_path / "a.c", mtime=3000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch marker
""",
            tmp_path,
        )
        result = runner.run("build")
        assert result.success
        assert (tmp_path / "marker").exists()

    def test_always_make_overrides_skip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch marker
""",
            tmp_path,
            always_make=True,
        )
        result = runner.run("build")
        assert result.success
        assert (tmp_path / "marker").exists()

    def test_skipped_dependency_does_not_block_dependents(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch build-marker

task package: build:
    !touch package-marker
""",
            tmp_path,
        )
        result = runner.run("package")
        assert result.success
        assert not (tmp_path / "build-marker").exists()
        assert (tmp_path / "package-marker").exists()

    def test_skip_records_artifact(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch marker
""",
            tmp_path,
        )
        runner.run("build")
        assert runner.artifacts["build"]["success"] == "skipped"

    def test_parallel_path_skips_too(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch marker

task other:
    !echo other

task all: build other:
    !echo done
""",
            tmp_path,
        )
        result = runner.run_parallel("all")
        assert result.success
        assert not (tmp_path / "marker").exists()

    def test_dry_run_never_skips(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _touch(tmp_path / "a.c", mtime=1000)
        _touch(tmp_path / "a.out", mtime=2000)
        runner = _runner(
            """\
task build [sources="a.c", outputs="a.out"]:
    !touch marker
""",
            tmp_path,
            dry_run=True,
        )
        result = runner.run("build")
        assert "[skipped]" not in result.task_results[0].response

    def test_source_change_invalidates_cache_key(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        src = _touch(tmp_path / "a.c", "one")
        runner = _runner(
            """\
task build [sources="a.c", cache="1h"]:
    !echo building
""",
            tmp_path,
        )
        task = runner.pf.tasks["build"]
        first = runner._cache_key(task)
        src.write_text("two")
        assert runner._cache_key(task) != first


class TestWatchPatterns:
    def test_collects_patterns_across_dependencies(self):
        from makethlm.cli import _watched_patterns

        pf = parse("""\
task compile [sources="src/*.c", outputs="app"]:
    !cc src/*.c

task docs [sources="docs/*.md", outputs="site"]:
    !build-docs

task all: compile docs:
    !echo done
""")
        assert _watched_patterns(pf, "all") == ["src/*.c", "docs/*.md"]

    def test_empty_without_sources(self):
        from makethlm.cli import _watched_patterns

        pf = parse("""\
task build:
    !make
""")
        assert _watched_patterns(pf, "build") == []

    def test_deduplicates_shared_patterns(self):
        from makethlm.cli import _watched_patterns

        pf = parse("""\
task a [sources="src/*.c", outputs="a.out"]:
    !cc

task b [sources="src/*.c", outputs="b.out"]:
    !cc

task all: a b:
    !echo done
""")
        assert _watched_patterns(pf, "all") == ["src/*.c"]

    def test_unknown_target_is_empty(self):
        from makethlm.cli import _watched_patterns

        pf = parse("""\
task build [sources="a.c", outputs="a.out"]:
    !cc
""")
        assert _watched_patterns(pf, "nope") == []


class TestWatchLoop:
    def test_reruns_when_a_watched_file_changes(self, tmp_path):
        from makethlm.cli import _watch_loop

        watched = _touch(tmp_path / "a.c", "one")
        promptfile = _touch(tmp_path / "Promptfile", "task a:\n    !echo hi\n")
        runs = []

        def run_once():
            runs.append(len(runs))
            if len(runs) >= 2:
                raise KeyboardInterrupt
            return 0

        # Change the file only after the loop has taken its first snapshot.
        timer = threading.Timer(0.1, lambda: watched.write_text("two"))
        timer.start()
        try:
            assert _watch_loop(run_once, [str(watched)], str(promptfile), 0.05) == 0
        finally:
            timer.cancel()
        assert len(runs) == 2

    def test_promptfile_edits_also_trigger_a_run(self, tmp_path):
        from makethlm.cli import _watch_loop

        watched = _touch(tmp_path / "a.c", "one")
        promptfile = _touch(tmp_path / "Promptfile", "task a:\n    !echo hi\n")
        runs = []

        def run_once():
            runs.append(len(runs))
            if len(runs) >= 2:
                raise KeyboardInterrupt
            return 0

        timer = threading.Timer(0.1, lambda: promptfile.write_text("task a:\n    !echo bye\n"))
        timer.start()
        try:
            _watch_loop(run_once, [str(watched)], str(promptfile), 0.05)
        finally:
            timer.cancel()
        assert len(runs) == 2

    def test_returns_last_exit_code_on_interrupt(self, tmp_path, monkeypatch):
        import makethlm.cli as cli_module
        from makethlm.cli import _watch_loop

        watched = _touch(tmp_path / "a.c", "one")
        promptfile = _touch(tmp_path / "Promptfile", "task a:\n    !echo hi\n")

        def interrupting_sleep(seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_module.time, "sleep", interrupting_sleep)
        assert _watch_loop(lambda: 3, [str(watched)], str(promptfile), 0.05) == 3
