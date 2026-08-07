"""Tests for `makethlm fmt`."""

from __future__ import annotations

from pathlib import Path

import pytest

from makethlm.cli import main
from makethlm.formatter import format_option_brackets, format_text, needs_formatting
from makethlm.parser import parse

EXAMPLES = sorted(Path(__file__).resolve().parent.parent.glob("examples/*/Promptfile"))


class TestIndentation:
    def test_normalizes_body_indent(self):
        src = "task build:\n  !make\n  compile it\n"
        assert format_text(src) == "task build:\n    !make\n    compile it\n"

    def test_normalizes_tabs(self):
        src = "task build:\n\t!make\n"
        assert format_text(src) == "task build:\n    !make\n"

    def test_preserves_relative_indent_in_script_bodies(self):
        src = 'task run [script("python3")]:\n  import sys\n  if sys.argv:\n      print(1)\n'
        formatted = format_text(src)
        assert "    import sys" in formatted
        assert "        print(1)" in formatted

    def test_leaves_column_zero_lines_alone(self):
        src = 'project := "x"\nversion := "1"\n'
        assert format_text(src) == src


class TestWhitespace:
    def test_strips_trailing_whitespace(self):
        assert format_text("task a:   \n    do it   \n") == "task a:\n    do it\n"

    def test_collapses_repeated_blank_lines(self):
        src = "task a:\n    do it\n\n\n\n\ntask b:\n    do it\n"
        assert format_text(src) == "task a:\n    do it\n\ntask b:\n    do it\n"

    def test_drops_leading_blank_lines(self):
        assert format_text("\n\ntask a:\n    do it\n") == "task a:\n    do it\n"

    def test_adds_a_trailing_newline(self):
        assert format_text("task a:\n    do it") == "task a:\n    do it\n"

    def test_preserves_deliberate_grouping(self):
        # Adjacent declarations must not be split apart.
        src = "set export\nset quiet\n\nllm claude [model=opus]\nllm openai [model=gpt-4]\n"
        assert format_text(src) == src

    def test_empty_file(self):
        assert format_text("") == ""
        assert format_text("\n\n\n") == ""


class TestOptionBrackets:
    def test_normalizes_spacing(self):
        assert format_option_brackets("task a [x=1,y=2]:") == "task a [x=1, y=2]:"

    def test_collapses_extra_spaces(self):
        assert format_option_brackets("task a [ x=1 ,  y=2 ]:") == "task a [x=1, y=2]:"

    def test_leaves_commas_inside_quotes(self):
        line = 'task a [confirm("really, truly?")]:'
        assert format_option_brackets(line) == line

    def test_leaves_commas_inside_parens(self):
        line = "task a [env(NAME, VALUE)]:"
        assert format_option_brackets(line) == line

    def test_applied_by_format_text(self):
        src = "task a [x=1,y=2]:\n    do it\n"
        assert format_text(src) == "task a [x=1, y=2]:\n    do it\n"


class TestSemanticsArePreserved:
    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
    def test_examples_are_already_formatted(self, path):
        assert not needs_formatting(path.read_text())

    def test_formatting_does_not_change_parsed_tasks(self):
        src = "task build [group=a,doc=b]:\n  !make\n\n\n\ntask test: build:\n\t!pytest\n"
        before = parse(src)
        after = parse(format_text(src))
        assert list(before.tasks) == list(after.tasks)
        for name, task in before.tasks.items():
            assert task.steps == after.tasks[name].steps
            assert task.dependencies == after.tasks[name].dependencies

    def test_is_idempotent(self):
        src = "task a [group=a,doc=b]:\n   do it\n\n\n\ntask b:\n\tdo it\n"
        once = format_text(src)
        assert format_text(once) == once


class TestFmtCommand:
    def _write(self, tmp_path, text):
        path = tmp_path / "Promptfile"
        path.write_text(text)
        return path

    def test_formats_in_place(self, tmp_path, monkeypatch, capsys):
        path = self._write(tmp_path, "task a:\n  do it\n")
        monkeypatch.chdir(tmp_path)
        assert main(["fmt"]) == 0
        assert path.read_text() == "task a:\n    do it\n"
        assert "reformatted" in capsys.readouterr().out

    def test_reports_already_formatted(self, tmp_path, monkeypatch, capsys):
        self._write(tmp_path, "task a:\n    do it\n")
        monkeypatch.chdir(tmp_path)
        assert main(["fmt"]) == 0
        assert "already formatted" in capsys.readouterr().out

    def test_check_mode_does_not_write(self, tmp_path, monkeypatch, capsys):
        original = "task a:\n  do it\n"
        path = self._write(tmp_path, original)
        monkeypatch.chdir(tmp_path)
        assert main(["fmt", "--check"]) == 1
        assert path.read_text() == original
        assert "would reformat" in capsys.readouterr().out

    def test_check_mode_passes_when_formatted(self, tmp_path, monkeypatch, capsys):
        self._write(tmp_path, "task a:\n    do it\n")
        monkeypatch.chdir(tmp_path)
        assert main(["fmt", "--check"]) == 0
        assert "all files are formatted" in capsys.readouterr().out

    def test_explicit_paths(self, tmp_path, monkeypatch):
        first = tmp_path / "one.pf"
        second = tmp_path / "two.pf"
        first.write_text("task a:\n  do it\n")
        second.write_text("task b:\n  do it\n")
        monkeypatch.chdir(tmp_path)
        assert main(["fmt", str(first), str(second)]) == 0
        assert first.read_text() == "task a:\n    do it\n"
        assert second.read_text() == "task b:\n    do it\n"

    def test_missing_file_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["fmt", str(tmp_path / "nope.pf")]) == 1
        assert "is not a file" in capsys.readouterr().err
