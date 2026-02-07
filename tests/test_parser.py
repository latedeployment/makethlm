"""Tests for the Promptfile parser."""

import pytest

from promptfile.parser import parse, ParseError


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------

class TestBasicParsing:
    def test_single_task(self):
        src = """\
task build:
    check if moo.md is newer, if so, build the docker from scratch
"""
        pf = parse(src)
        assert "build" in pf.tasks
        assert pf.tasks["build"].prompt == "check if moo.md is newer, if so, build the docker from scratch"
        assert pf.tasks["build"].dependencies == []

    def test_multiline_prompt(self):
        src = """\
task refactor:
    look at src/ and find functions over 50 lines.
    refactor them into smaller functions.
    run tests after each change.
"""
        pf = parse(src)
        expected = (
            "look at src/ and find functions over 50 lines.\n"
            "refactor them into smaller functions.\n"
            "run tests after each change."
        )
        assert pf.tasks["refactor"].prompt == expected

    def test_multiple_tasks(self):
        src = """\
task build:
    build the project

task test:
    run all tests

task deploy:
    deploy to production
"""
        pf = parse(src)
        assert list(pf.task_order) == ["build", "test", "deploy"]
        assert len(pf.tasks) == 3

    def test_default_task_is_first(self):
        src = """\
task alpha:
    do alpha thing

task beta:
    do beta thing
"""
        pf = parse(src)
        assert pf.default_task == "alpha"

    def test_empty_promptfile_has_no_default(self):
        pf = parse("")
        assert pf.default_task is None
        assert len(pf.tasks) == 0


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

class TestVariables:
    def test_variable_definition(self):
        src = """\
project := "myapp"

task build:
    build {{project}}
"""
        pf = parse(src)
        assert pf.variables == {"project": "myapp"}

    def test_variable_interpolation(self):
        src = """\
project := "myapp"
env := "staging"

task deploy:
    deploy {{project}} to {{env}}
"""
        pf = parse(src)
        assert pf.resolve_prompt("deploy") == "deploy myapp to staging"

    def test_escaped_quotes_in_variable(self):
        src = r'''
name := "hello \"world\""

task greet:
    say {{name}}
'''
        pf = parse(src)
        assert pf.variables["name"] == 'hello "world"'

    def test_multiple_variables(self):
        src = """\
a := "one"
b := "two"
c := "three"

task show:
    show {{a}} {{b}} {{c}}
"""
        pf = parse(src)
        assert pf.resolve_prompt("show") == "show one two three"


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_single_dependency(self):
        src = """\
task build:
    build the project

task deploy: build:
    deploy it
"""
        pf = parse(src)
        assert pf.tasks["deploy"].dependencies == ["build"]

    def test_multiple_dependencies(self):
        src = """\
task build:
    build

task test:
    test

task deploy: build test:
    deploy
"""
        pf = parse(src)
        assert pf.tasks["deploy"].dependencies == ["build", "test"]

    def test_unknown_dependency_raises(self):
        src = """\
task deploy: nonexistent:
    deploy
"""
        with pytest.raises(ParseError, match="unknown task"):
            parse(src)


# ---------------------------------------------------------------------------
# Task options
# ---------------------------------------------------------------------------

class TestTaskOptions:
    def test_model_option(self):
        src = """\
task review [model=opus]:
    review the code
"""
        pf = parse(src)
        assert pf.tasks["review"].options.model == "opus"

    def test_temperature_option(self):
        src = """\
task creative [temperature=0.9]:
    write a poem
"""
        pf = parse(src)
        assert pf.tasks["creative"].options.temperature == 0.9

    def test_multiple_options(self):
        src = """\
task review [model=opus, temperature=0.2, max_tokens=4096]:
    review carefully
"""
        pf = parse(src)
        opts = pf.tasks["review"].options
        assert opts.model == "opus"
        assert opts.temperature == 0.2
        assert opts.max_tokens == 4096

    def test_options_with_dependencies(self):
        src = """\
task build:
    build it

task deploy: build [model=haiku]:
    deploy quickly
"""
        pf = parse(src)
        assert pf.tasks["deploy"].dependencies == ["build"]
        assert pf.tasks["deploy"].options.model == "haiku"

    def test_unknown_option_raises(self):
        src = """\
task bad [bogus=123]:
    do stuff
"""
        with pytest.raises(ParseError, match="unknown option"):
            parse(src)

    def test_invalid_temperature_raises(self):
        src = """\
task bad [temperature=hot]:
    do stuff
"""
        with pytest.raises(ParseError, match="temperature must be a number"):
            parse(src)


# ---------------------------------------------------------------------------
# Comments and whitespace
# ---------------------------------------------------------------------------

class TestCommentsAndWhitespace:
    def test_comments_are_ignored(self):
        src = """\
# This is a comment
task build:
    # This is inside the task prompt area but indented
    build the project
"""
        pf = parse(src)
        # The indented '# This is inside...' becomes part of the prompt
        assert "build the project" in pf.tasks["build"].prompt

    def test_blank_lines_between_tasks(self):
        src = """\
task a:
    do a



task b:
    do b
"""
        pf = parse(src)
        assert len(pf.tasks) == 2

    def test_blank_lines_within_prompt(self):
        src = """\
task complex:
    first paragraph of instructions.

    second paragraph of instructions.
"""
        pf = parse(src)
        assert "first paragraph" in pf.tasks["complex"].prompt
        assert "second paragraph" in pf.tasks["complex"].prompt


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrors:
    def test_duplicate_task(self):
        src = """\
task build:
    first

task build:
    second
"""
        with pytest.raises(ParseError, match="duplicate task"):
            parse(src)

    def test_task_with_no_body(self):
        src = """\
task empty:
"""
        with pytest.raises(ParseError, match="no prompt body"):
            parse(src)

    def test_garbage_line(self):
        src = """\
this is not valid syntax
"""
        with pytest.raises(ParseError, match="unexpected line"):
            parse(src)

    def test_task_no_body_followed_by_task(self):
        src = """\
task first:
task second:
    do stuff
"""
        with pytest.raises(ParseError, match="no prompt body"):
            parse(src)


# ---------------------------------------------------------------------------
# Full integration-style parse
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_realistic_promptfile(self):
        src = """\
# Project configuration
project := "my-web-app"
env := "staging"

# Check if files changed and rebuild
task build:
    check if moo.md is newer than the Dockerfile.
    if so, rebuild the docker image from scratch.
    tag it as {{project}}:latest.

# Run the test suite
task test: build:
    run the test suite for {{project}}.
    report any failures clearly.

# Security review
task review [model=opus, temperature=0.1]:
    review the git diff for {{project}} carefully.
    look for security vulnerabilities, especially:
    - SQL injection
    - XSS
    - command injection

# Deploy to environment
task deploy: build test [model=haiku]:
    deploy {{project}} to {{env}}.
    verify the health check passes.
"""
        pf = parse(src)

        assert pf.variables == {"project": "my-web-app", "env": "staging"}
        assert list(pf.task_order) == ["build", "test", "review", "deploy"]

        assert pf.tasks["test"].dependencies == ["build"]
        assert pf.tasks["deploy"].dependencies == ["build", "test"]

        assert pf.tasks["review"].options.model == "opus"
        assert pf.tasks["review"].options.temperature == 0.1
        assert pf.tasks["deploy"].options.model == "haiku"

        resolved = pf.resolve_prompt("deploy")
        assert "my-web-app" in resolved
        assert "staging" in resolved
        assert "{{project}}" not in resolved
