"""Tests for the Promptfile parser."""

import os
import tempfile

import pytest

from makethlm.parser import ParseError, parse

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
        assert (
            pf.tasks["build"].prompt
            == "check if moo.md is newer, if so, build the docker from scratch"
        )
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

    def test_set_default_overrides_first_task(self):
        src = """\
set default beta

task alpha:
    do alpha thing

task beta:
    do beta thing
"""
        pf = parse(src)
        assert pf.default_task == "beta"

    def test_set_default_unknown_task_raises(self):
        src = """\
set default nonexistent

task alpha:
    do alpha thing
"""
        with pytest.raises(ParseError, match="unknown task"):
            parse(src)


class TestJustStyleRecipes:
    def test_bare_recipe_becomes_shell_task(self):
        src = """\
build:
    echo build
"""
        pf = parse(src)
        assert "build" in pf.tasks
        steps = pf.resolve_steps("build")
        assert len(steps) == 1
        assert steps[0].kind == "shell"
        assert steps[0].content == "echo build"

    def test_bare_recipe_dependencies_without_trailing_colon(self):
        src = """\
build:
    echo build

test: build
    echo test
"""
        pf = parse(src)
        assert pf.tasks["test"].dependencies == ["build"]

    def test_bare_recipe_arguments(self):
        src = """\
deploy target port="8080":
    echo {{target}} {{port}}
"""
        pf = parse(src)
        args = pf.tasks["deploy"].arguments
        assert [arg.name for arg in args] == ["target", "port"]
        assert args[1].default == "8080"
        steps = pf.resolve_steps("deploy", args={"target": "prod"})
        assert steps[0].content == "echo prod 8080"

    def test_bare_recipe_quiet_and_ignore_prefixes(self):
        src = """\
clean:
    @echo quiet
    -rm missing
    @-rm also-missing
"""
        pf = parse(src)
        steps = pf.tasks["clean"].steps
        assert steps[0].quiet is True
        assert steps[0].content == "echo quiet"
        assert steps[1].ignore_error is True
        assert steps[1].content == "rm missing"
        assert steps[2].quiet is True
        assert steps[2].ignore_error is True
        assert steps[2].content == "rm also-missing"

    def test_bare_recipe_underscore_is_private(self):
        src = """\
_helper:
    echo hidden
"""
        pf = parse(src)
        assert pf.tasks["_helper"].options.private is True

    def test_bare_recipe_shebang_script(self):
        src = """\
script:
    #!/usr/bin/env python3
    print("hello")
"""
        pf = parse(src)
        step = pf.tasks["script"].steps[0]
        assert step.script is True
        assert "python3" in step.content

    def test_subsequent_dependencies(self):
        src = """\
build: setup && notify
    echo build

setup:
    echo setup

notify:
    echo notify
"""
        pf = parse(src)
        assert pf.tasks["build"].dependencies == ["setup"]
        assert pf.tasks["build"].subsequent_dependencies == ["notify"]

    def test_shell_array_setting(self):
        src = """\
set shell := ["bash", "-cu"]

build:
    echo build
"""
        pf = parse(src)
        assert pf.settings.shell_argv == ["bash", "-cu"]

    def test_script_attribute(self):
        src = """\
build [script("python3"), extension("py")]:
    print("build")
"""
        pf = parse(src)
        task = pf.tasks["build"]
        assert task.options.script is True
        assert task.options.script_command == "python3"
        assert task.options.extension == "py"
        assert task.steps[0].script is True


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
        src = r"""
name := "hello \"world\""

task greet:
    say {{name}}
"""
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
    build the project
"""
        pf = parse(src)
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
# Shell commands with !
# ---------------------------------------------------------------------------


class TestShellCommands:
    def test_single_shell_command(self):
        src = """\
task clean:
    !rm -rf build/
"""
        pf = parse(src)
        steps = pf.tasks["clean"].steps
        assert len(steps) == 1
        assert steps[0].kind == "shell"
        assert steps[0].content == "rm -rf build/"

    def test_multiple_shell_commands(self):
        src = """\
task setup:
    !mkdir -p dist
    !npm install
    !npm run build
"""
        pf = parse(src)
        steps = pf.tasks["setup"].steps
        assert len(steps) == 3
        assert all(s.kind == "shell" for s in steps)
        assert steps[0].content == "mkdir -p dist"
        assert steps[1].content == "npm install"
        assert steps[2].content == "npm run build"

    def test_interleaved_shell_and_prompt(self):
        src = """\
task analyze:
    !git diff --name-only > /tmp/changed.txt
    review the changed files in /tmp/changed.txt
    !npm test
"""
        pf = parse(src)
        steps = pf.tasks["analyze"].steps
        assert len(steps) == 3
        assert steps[0].kind == "shell"
        assert steps[0].content == "git diff --name-only > /tmp/changed.txt"
        assert steps[1].kind == "prompt"
        assert steps[1].content == "review the changed files in /tmp/changed.txt"
        assert steps[2].kind == "shell"
        assert steps[2].content == "npm test"

    def test_consecutive_prompt_lines_merge(self):
        src = """\
task review:
    look at the code carefully.
    check for security issues.
    !npm audit
    review the npm audit output.
"""
        pf = parse(src)
        steps = pf.tasks["review"].steps
        assert len(steps) == 3
        assert steps[0].kind == "prompt"
        assert steps[0].content == "look at the code carefully.\ncheck for security issues."
        assert steps[1].kind == "shell"
        assert steps[1].content == "npm audit"
        assert steps[2].kind == "prompt"
        assert steps[2].content == "review the npm audit output."

    def test_silent_prefix(self):
        src = """\
task clean:
    !@silent rm -rf build/
"""
        pf = parse(src)
        step = pf.tasks["clean"].steps[0]
        assert step.kind == "shell"
        assert step.silent is True
        assert step.ignore_error is False
        assert step.content == "rm -rf build/"

    def test_ignore_prefix(self):
        src = """\
task cleanup:
    !@ignore docker rm old-container
"""
        pf = parse(src)
        step = pf.tasks["cleanup"].steps[0]
        assert step.kind == "shell"
        assert step.silent is False
        assert step.ignore_error is True
        assert step.content == "docker rm old-container"

    def test_both_silent_and_ignore(self):
        src = """\
task cleanup:
    !@silent @ignore docker system prune -f
"""
        pf = parse(src)
        step = pf.tasks["cleanup"].steps[0]
        assert step.kind == "shell"
        assert step.silent is True
        assert step.ignore_error is True
        assert step.content == "docker system prune -f"

    def test_prompt_property_excludes_shell(self):
        src = """\
task build:
    !mkdir -p dist
    compile the project
    !npm test
"""
        pf = parse(src)
        # .prompt should only include the prompt text, not shell commands
        assert pf.tasks["build"].prompt == "compile the project"

    def test_has_shell_steps(self):
        src = """\
task a:
    !echo hi

task b:
    just a prompt
"""
        pf = parse(src)
        assert pf.tasks["a"].has_shell_steps is True
        assert pf.tasks["b"].has_shell_steps is False

    def test_echo_step(self):
        pf = parse("""\
task build:
    @echo "Building project..."
    !make build
""")
        steps = pf.tasks["build"].steps
        assert len(steps) == 2
        assert steps[0].kind == "echo"
        assert steps[0].content == "Building project..."
        assert steps[1].kind == "shell"

    def test_echo_without_quotes(self):
        pf = parse("""\
task deploy:
    @echo deploying now
""")
        steps = pf.tasks["deploy"].steps
        assert steps[0].kind == "echo"
        assert steps[0].content == "deploying now"

    def test_echo_single_quotes(self):
        pf = parse("""\
task test:
    @echo 'running tests'
""")
        steps = pf.tasks["test"].steps
        assert steps[0].kind == "echo"
        assert steps[0].content == "running tests"

    def test_echo_with_variable_interpolation(self):
        pf = parse("""\
version := "1.0.0"

task release:
    @echo "Releasing version {{version}}"
""")
        resolved = pf.resolve_steps("release")
        assert resolved[0].kind == "echo"
        assert resolved[0].content == "Releasing version 1.0.0"

    def test_echo_between_steps(self):
        pf = parse("""\
task pipeline:
    !step1
    @echo "Step 1 done, running step 2..."
    !step2
    summarize the results
""")
        steps = pf.tasks["pipeline"].steps
        assert len(steps) == 4
        assert steps[0].kind == "shell"
        assert steps[1].kind == "echo"
        assert steps[2].kind == "shell"
        assert steps[3].kind == "prompt"

    def test_shell_step_capture_syntax(self):
        pf = parse("""\
task analyze:
    !git diff --name-only -> changed
    review {{changed.stdout}}
""")
        step = pf.tasks["analyze"].steps[0]
        assert step.kind == "shell"
        assert step.content == "git diff --name-only"
        assert step.capture == "changed"

    def test_shell_step_pipe_syntax(self):
        pf = parse("""\
task analyze:
    !git diff --name-only |>
    review changed files
""")
        steps = pf.tasks["analyze"].steps
        assert steps[0].kind == "shell"
        assert steps[0].content == "git diff --name-only"
        assert steps[0].pipe_output is True
        assert steps[1].kind == "prompt"

    def test_shell_step_inline_pipe_prompt(self):
        pf = parse("""\
task analyze:
    !git diff --name-only |> review changed files
""")
        steps = pf.tasks["analyze"].steps
        assert len(steps) == 2
        assert steps[0].kind == "shell"
        assert steps[0].pipe_output is True
        assert steps[1].kind == "prompt"
        assert steps[1].content == "review changed files"


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


class TestFunctions:
    def test_basic_function(self):
        src = """\
fn greet:
    say hello to the user

task hello:
    @use greet
"""
        pf = parse(src)
        assert "greet" in pf.functions
        assert pf.functions["greet"].body == "say hello to the user"

    def test_multiline_function(self):
        src = """\
fn security_review:
    Review the code for security vulnerabilities.
    Check for SQL injection, XSS, and command injection.
    Be concise and actionable.

task review:
    @use security_review
    Focus on the current PR.
"""
        pf = parse(src)
        fn = pf.functions["security_review"]
        assert "SQL injection" in fn.body
        assert "XSS" in fn.body
        assert fn.body.count("\n") == 2  # 3 lines

    def test_use_expands_function(self):
        src = """\
fn preamble:
    You are a helpful code reviewer.

task review:
    @use preamble
    Review this file.
"""
        pf = parse(src)
        resolved = pf.resolve_prompt("review")
        assert resolved == "You are a helpful code reviewer.\nReview this file."

    def test_use_unknown_function_raises(self):
        src = """\
task bad:
    @use nonexistent
"""
        with pytest.raises(ParseError, match="unknown function"):
            parse(src)

    def test_duplicate_function_raises(self):
        src = """\
fn greet:
    hello

fn greet:
    hello again
"""
        with pytest.raises(ParseError, match="duplicate function"):
            parse(src)

    def test_empty_function_raises(self):
        src = """\
fn empty:
"""
        with pytest.raises(ParseError, match="no body"):
            parse(src)

    def test_multiple_uses_in_one_task(self):
        src = """\
fn header:
    Act as a senior engineer.

fn footer:
    Be thorough.

task review:
    @use header
    Review the code.
    @use footer
"""
        pf = parse(src)
        resolved = pf.resolve_prompt("review")
        assert resolved == "Act as a senior engineer.\nReview the code.\nBe thorough."


# ---------------------------------------------------------------------------
# Task arguments
# ---------------------------------------------------------------------------


class TestTaskArguments:
    def test_single_required_arg(self):
        src = """\
task deploy(target):
    deploy to {{target}}
"""
        pf = parse(src)
        task = pf.tasks["deploy"]
        assert len(task.arguments) == 1
        assert task.arguments[0].name == "target"
        assert task.arguments[0].default is None

    def test_arg_with_default(self):
        src = """\
task deploy(target, port="8080"):
    deploy to {{target}} on port {{port}}
"""
        pf = parse(src)
        task = pf.tasks["deploy"]
        assert len(task.arguments) == 2
        assert task.arguments[0].name == "target"
        assert task.arguments[0].default is None
        assert task.arguments[1].name == "port"
        assert task.arguments[1].default == "8080"

    def test_arg_interpolation(self):
        src = """\
task greet(name):
    say hello to {{name}}
"""
        pf = parse(src)
        resolved = pf.resolve_prompt("greet", args={"name": "Alice"})
        assert resolved == "say hello to Alice"

    def test_arg_default_used_when_not_provided(self):
        src = """\
task deploy(target="localhost"):
    deploy to {{target}}
"""
        pf = parse(src)
        resolved = pf.resolve_prompt("deploy")
        assert resolved == "deploy to localhost"

    def test_args_with_dependencies(self):
        src = """\
task build:
    build

task deploy(env): build:
    deploy to {{env}}
"""
        pf = parse(src)
        task = pf.tasks["deploy"]
        assert task.arguments[0].name == "env"
        assert task.dependencies == ["build"]

    def test_args_with_options(self):
        src = """\
task deploy(target) [model=haiku]:
    deploy to {{target}}
"""
        pf = parse(src)
        task = pf.tasks["deploy"]
        assert task.arguments[0].name == "target"
        assert task.options.model == "haiku"

    def test_args_deps_and_options(self):
        src = """\
task build:
    build

task deploy(env, region="us-east"): build [model=haiku]:
    deploy to {{env}} in {{region}}
"""
        pf = parse(src)
        task = pf.tasks["deploy"]
        assert task.arguments[0].name == "env"
        assert task.arguments[1].default == "us-east"
        assert task.dependencies == ["build"]
        assert task.options.model == "haiku"


# ---------------------------------------------------------------------------
# Docker blocks
# ---------------------------------------------------------------------------


class TestDockerBlocks:
    def test_basic_docker(self):
        src = """\
docker myapp:
    Python 3.11 slim image.
    Install requirements.txt.
    Expose port 8080.
"""
        pf = parse(src)
        assert "myapp" in pf.tasks
        task = pf.tasks["myapp"]
        assert task.docker is not None
        assert task.docker.tag == "latest"
        assert task.docker.context == "."
        assert task.docker.file == "Dockerfile"
        assert "Python 3.11" in task.prompt

    def test_docker_with_options(self):
        src = """\
docker myapp [tag=v2, context=./app, file=Dockerfile.prod]:
    Python 3.11 slim image.
"""
        pf = parse(src)
        docker = pf.tasks["myapp"].docker
        assert docker is not None
        assert docker.tag == "v2"
        assert docker.context == "./app"
        assert docker.file == "Dockerfile.prod"

    def test_docker_as_dependency(self):
        src = """\
docker myapp:
    Python 3.11 slim image.

task deploy: myapp:
    push the image
"""
        pf = parse(src)
        assert pf.tasks["deploy"].dependencies == ["myapp"]

    def test_docker_appears_in_task_order(self):
        src = """\
docker api:
    Node 20 alpine image.

task test:
    run tests
"""
        pf = parse(src)
        assert "api" in pf.task_order
        assert pf.task_order == ["api", "test"]

    def test_empty_docker_raises(self):
        src = """\
docker myapp:
"""
        with pytest.raises(ParseError, match="no description"):
            parse(src)

    def test_duplicate_docker_raises(self):
        src = """\
docker myapp:
    first

docker myapp:
    second
"""
        with pytest.raises(ParseError, match="duplicate"):
            parse(src)

    def test_docker_and_task_name_collision(self):
        src = """\
task myapp:
    do stuff

docker myapp:
    image
"""
        with pytest.raises(ParseError, match="duplicate"):
            parse(src)


# ---------------------------------------------------------------------------
# Include directive
# ---------------------------------------------------------------------------


class TestIncludes:
    def test_include_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            common = os.path.join(tmpdir, "common.pf")
            with open(common, "w") as f:
                f.write('env := "production"\n\ntask setup:\n    setup env\n')

            main_src = 'include "common.pf"\n\ntask deploy: setup:\n    deploy\n'
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

            assert pf.variables["env"] == "production"
            assert "setup" in pf.tasks
            assert "deploy" in pf.tasks

    def test_include_functions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib = os.path.join(tmpdir, "lib.pf")
            with open(lib, "w") as f:
                f.write("fn greet:\n    say hello\n")

            main_src = 'include "lib.pf"\n\ntask hello:\n    @use greet\n'
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

            assert "greet" in pf.functions
            resolved = pf.resolve_prompt("hello")
            assert resolved == "say hello"

    def test_include_not_found_raises(self):
        src = 'include "nonexistent.pf"\n'
        with pytest.raises(ParseError, match="not found"):
            parse(src)

    def test_import_alias_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            common = os.path.join(tmpdir, "common.pf")
            with open(common, "w") as f:
                f.write('env := "production"\n\ntask setup:\n    setup env\n')

            main_src = 'import "common.pf"\n\ntask deploy: setup:\n    deploy\n'
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

            assert pf.variables["env"] == "production"
            assert "setup" in pf.tasks

    def test_optional_import_missing_is_ignored(self):
        src = """\
import? "missing.pf"

task build:
    build it
"""
        pf = parse(src)
        assert "build" in pf.tasks

    def test_mod_prefixes_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            module = os.path.join(tmpdir, "ops.pf")
            with open(module, "w") as f:
                f.write("""\
task deploy:
    deploy module
""")

            pf = parse('mod ops "ops.pf"\n', filename=os.path.join(tmpdir, "Promptfile"))

            assert "ops::deploy" in pf.tasks
            assert pf.tasks["ops::deploy"].name == "ops::deploy"

    def test_mod_prefixes_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            module = os.path.join(tmpdir, "ops.pf")
            with open(module, "w") as f:
                f.write("""\
task deploy:
    deploy module

alias d := deploy
""")

            pf = parse('mod ops "ops.pf"\n', filename=os.path.join(tmpdir, "Promptfile"))

            assert pf.aliases["ops::d"] == "ops::deploy"
            assert pf.resolve_alias("ops::d") == "ops::deploy"

    def test_circular_include_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            a = os.path.join(tmpdir, "a.pf")
            b = os.path.join(tmpdir, "b.pf")
            with open(a, "w") as f:
                f.write('include "b.pf"\n\ntask x:\n    do x\n')
            with open(b, "w") as f:
                f.write('include "a.pf"\n\ntask y:\n    do y\n')

            with pytest.raises(ParseError, match="circular include"):
                with open(a) as f:
                    parse(f.read(), filename=a)

    def test_local_overrides_included(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            common = os.path.join(tmpdir, "common.pf")
            with open(common, "w") as f:
                f.write('env := "default"\n')

            main_src = 'include "common.pf"\nenv := "override"\n\ntask show:\n    env is {{env}}\n'
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

            # Local definition should override included
            assert pf.variables["env"] == "override"


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


class TestEnvVars:
    def test_env_var_in_prompt(self):
        os.environ["PF_TEST_VAR"] = "hello"
        try:
            src = """\
task greet:
    say ${PF_TEST_VAR}
"""
            pf = parse(src)
            resolved = pf.resolve_prompt("greet")
            assert resolved == "say hello"
        finally:
            del os.environ["PF_TEST_VAR"]

    def test_env_var_with_default(self):
        # Make sure variable is NOT set
        os.environ.pop("PF_UNSET_VAR", None)
        src = """\
task show:
    value is ${PF_UNSET_VAR:-fallback}
"""
        pf = parse(src)
        resolved = pf.resolve_prompt("show")
        assert resolved == "value is fallback"

    def test_env_var_with_default_when_set(self):
        os.environ["PF_SET_VAR"] = "real"
        try:
            src = """\
task show:
    value is ${PF_SET_VAR:-fallback}
"""
            pf = parse(src)
            resolved = pf.resolve_prompt("show")
            assert resolved == "value is real"
        finally:
            del os.environ["PF_SET_VAR"]


# ---------------------------------------------------------------------------
# Full integration-style parse
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_realistic_promptfile(self):
        src = """\
# Project configuration
project := "my-web-app"
env := "staging"

fn code_review:
    Review the code for quality and security.
    Be concise.

# Check if files changed and rebuild
task build:
    !mkdir -p dist
    check if moo.md is newer than the Dockerfile.
    if so, rebuild the docker image from scratch.
    tag it as {{project}}:latest.

# Run the test suite
task test: build:
    !npm test
    report any failures clearly.

# Security review
task review [model=opus, temperature=0.1]:
    @use code_review
    Focus on {{project}}.

# Deploy with arguments
task deploy(target, port="443"): build test [model=haiku]:
    deploy {{project}} to {{target}} on port {{port}}.
    verify the health check passes.

# Docker image
docker api [tag=v1]:
    Node 20 alpine image.
    Install package.json.
    Copy src/.
    Expose 3000.
"""
        pf = parse(src)

        assert pf.variables == {"project": "my-web-app", "env": "staging"}
        assert "code_review" in pf.functions
        assert list(pf.task_order) == ["build", "test", "review", "deploy", "api"]

        # Build has interleaved shell + prompt
        build_steps = pf.tasks["build"].steps
        assert build_steps[0].kind == "shell"
        assert build_steps[0].content == "mkdir -p dist"
        assert build_steps[1].kind == "prompt"

        # Test has shell + prompt
        test_steps = pf.tasks["test"].steps
        assert test_steps[0].kind == "shell"
        assert test_steps[1].kind == "prompt"

        # Review uses function
        assert pf.tasks["test"].dependencies == ["build"]
        assert pf.tasks["deploy"].dependencies == ["build", "test"]

        assert pf.tasks["review"].options.model == "opus"
        assert pf.tasks["review"].options.temperature == 0.1
        assert pf.tasks["deploy"].options.model == "haiku"

        # Deploy has arguments
        assert pf.tasks["deploy"].arguments[0].name == "target"
        assert pf.tasks["deploy"].arguments[1].default == "443"

        # Docker block
        assert pf.tasks["api"].docker is not None
        assert pf.tasks["api"].docker.tag == "v1"

        # Resolve with args
        resolved = pf.resolve_prompt("deploy", args={"target": "prod"})
        assert "my-web-app" in resolved
        assert "prod" in resolved
        assert "443" in resolved  # default

        # Resolve review expands @use
        review_resolved = pf.resolve_prompt("review")
        assert "Review the code for quality" in review_resolved
        assert "my-web-app" in review_resolved


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


class TestLLMProviders:
    def test_basic_llm_declaration(self):
        src = """\
llm claude

task build:
    build it
"""
        pf = parse(src)
        assert "claude" in pf.llm_providers
        assert pf.default_llm == "claude"
        assert pf.llm_providers["claude"].name == "claude"

    def test_llm_with_options(self):
        src = """\
llm openai [model=gpt-4, base_url=https://api.openai.com]

task build:
    build it
"""
        pf = parse(src)
        prov = pf.llm_providers["openai"]
        assert prov.model == "gpt-4"
        assert prov.base_url == "https://api.openai.com"

    def test_multiple_llm_providers(self):
        src = """\
llm claude [model=opus]
llm openai [model=gpt-4]
llm ollama [model=llama3]

task build:
    build it
"""
        pf = parse(src)
        assert len(pf.llm_providers) == 3
        # First declared is default
        assert pf.default_llm == "claude"

    def test_per_task_llm_override(self):
        src = """\
llm claude [model=opus]
llm openai [model=gpt-4]

task fast-task [llm=openai]:
    do something fast

task default-task:
    use default llm
"""
        pf = parse(src)
        assert pf.tasks["fast-task"].options.llm == "openai"
        assert pf.tasks["default-task"].options.llm is None

        # get_llm_for_task resolves correctly
        fast_provider = pf.get_llm_for_task("fast-task")
        assert fast_provider is not None
        assert fast_provider.name == "openai"
        default_provider = pf.get_llm_for_task("default-task")
        assert default_provider is not None
        assert default_provider.name == "claude"  # falls back to default

    def test_llm_with_shell_template(self):
        src = """\
llm custom [template=my-llm run {prompt}]

task build:
    build it
"""
        pf = parse(src)
        assert pf.llm_providers["custom"].shell_template == "my-llm run {prompt}"

    def test_llm_with_env_key(self):
        os.environ["PF_TEST_KEY"] = "sk-test123"
        try:
            src = """\
llm openai [model=gpt-4, key=$PF_TEST_KEY]

task build:
    build it
"""
            pf = parse(src)
            assert pf.llm_providers["openai"].api_key == "sk-test123"
        finally:
            del os.environ["PF_TEST_KEY"]


# ---------------------------------------------------------------------------
# Host groups
# ---------------------------------------------------------------------------


class TestHostGroups:
    def test_basic_host_group(self):
        src = """\
hosts web:
    web1.example.com
    web2.example.com

task deploy [on=web]:
    !systemctl restart myapp
"""
        pf = parse(src)
        assert "web" in pf.host_groups
        group = pf.host_groups["web"]
        assert group.hosts == ["web1.example.com", "web2.example.com"]
        assert pf.tasks["deploy"].options.on == "web"

    def test_host_group_with_options(self):
        src = """\
hosts db [user=postgres, port=2222]:
    db-primary.internal
    db-replica.internal

task backup [on=db]:
    !pg_dump mydb
"""
        pf = parse(src)
        group = pf.host_groups["db"]
        assert group.user == "postgres"
        assert group.port == 2222
        assert group.hosts == ["db-primary.internal", "db-replica.internal"]

    def test_host_group_with_ssh_options(self):
        src = """\
hosts web [identity-file=/tmp/id_ed25519, strict-host-key-checking=no]:
    web1.example.com

task deploy [on=web]:
    !uptime
"""
        pf = parse(src)
        group = pf.host_groups["web"]
        assert group.identity_file == "/tmp/id_ed25519"
        assert group.strict_host_key_checking == "no"

    def test_multiple_host_groups(self):
        src = """\
hosts web:
    web1.example.com

hosts db:
    db1.example.com

task deploy [on=web]:
    !deploy.sh
"""
        pf = parse(src)
        assert len(pf.host_groups) == 2

    def test_unknown_host_group_raises(self):
        src = """\
task deploy [on=nonexistent]:
    !deploy.sh
"""
        with pytest.raises(ParseError, match="unknown host group"):
            parse(src)

    def test_empty_host_group_raises(self):
        src = """\
hosts empty:
"""
        with pytest.raises(ParseError, match="no hosts"):
            parse(src)

    def test_duplicate_host_group_raises(self):
        src = """\
hosts web:
    host1

hosts web:
    host2
"""
        with pytest.raises(ParseError, match="duplicate host group"):
            parse(src)

    def test_get_hosts_for_task(self):
        src = """\
hosts web:
    web1
    web2

task local:
    do locally

task remote [on=web]:
    !uptime
"""
        pf = parse(src)
        assert pf.get_hosts_for_task("local") is None
        group = pf.get_hosts_for_task("remote")
        assert group is not None
        assert group.hosts == ["web1", "web2"]

    def test_host_group_with_ip_addresses(self):
        src = """\
hosts cluster:
    192.168.1.10
    192.168.1.11
    10.0.0.5

task check [on=cluster]:
    !hostname
"""
        pf = parse(src)
        assert len(pf.host_groups["cluster"].hosts) == 3


# ---------------------------------------------------------------------------
# Full integration with all features
# ---------------------------------------------------------------------------


class TestFullIntegration:
    def test_everything_together(self):
        src = """\
project := "megaapp"

llm claude [model=opus]
llm openai [model=gpt-4]

hosts web [user=deploy]:
    web1.prod.internal
    web2.prod.internal

hosts db [user=postgres, port=5433]:
    db-primary.prod.internal

fn check_health:
    Verify all services are healthy.
    Report any issues.

task build:
    !npm run build
    check the build output for warnings.

task deploy: build [llm=openai, on=web]:
    !systemctl restart {{project}}
    @use check_health

task backup [on=db]:
    !pg_dump {{project}} > /tmp/backup.sql

docker api-image [tag=v2]:
    Node 20 alpine with npm ci.
"""
        pf = parse(src)

        assert pf.variables == {"project": "megaapp"}
        assert pf.default_llm == "claude"
        assert len(pf.llm_providers) == 2

        assert pf.tasks["deploy"].options.llm == "openai"
        assert pf.tasks["deploy"].options.on == "web"
        assert pf.tasks["backup"].options.on == "db"

        assert pf.host_groups["web"].user == "deploy"
        assert pf.host_groups["db"].port == 5433

        # LLM resolution
        deploy_llm = pf.get_llm_for_task("deploy")
        assert deploy_llm is not None
        assert deploy_llm.name == "openai"
        build_llm = pf.get_llm_for_task("build")
        assert build_llm is not None
        assert build_llm.name == "claude"  # default

        # Host resolution
        deploy_hosts = pf.get_hosts_for_task("deploy")
        assert deploy_hosts is not None
        assert deploy_hosts.name == "web"
        backup_hosts = pf.get_hosts_for_task("backup")
        assert backup_hosts is not None
        assert backup_hosts.name == "db"
        assert pf.get_hosts_for_task("build") is None


# ---------------------------------------------------------------------------
# Set directives
# ---------------------------------------------------------------------------


class TestSetDirectives:
    def test_set_dotenv_load(self):
        src = """\
set dotenv-load

task build:
    build it
"""
        pf = parse(src)
        assert pf.settings.dotenv_load is True

    def test_set_dotenv_load_false(self):
        src = """\
set dotenv-load false

task build:
    build it
"""
        pf = parse(src)
        assert pf.settings.dotenv_load is False

    def test_set_shell(self):
        src = """\
set shell "/bin/bash"

task build:
    build it
"""
        pf = parse(src)
        assert pf.settings.shell == "/bin/bash"

    def test_set_shell_single_quotes(self):
        src = """\
set shell '/bin/zsh'

task build:
    build it
"""
        pf = parse(src)
        assert pf.settings.shell == "/bin/zsh"

    def test_set_working_dir(self):
        src = """\
set working-dir "/tmp/myproject"

task build:
    build it
"""
        pf = parse(src)
        assert pf.settings.working_dir == "/tmp/myproject"

    def test_set_secrets_backend(self):
        src = """\
set secrets "infisical"
set secrets-project "my-project"
set secrets-environment "production"
set secrets-vault "DevOps"
set secrets-file "secrets.yaml"

task build:
    build it
"""
        pf = parse(src)
        assert pf.settings.secrets == "infisical"
        assert pf.settings.secrets_project == "my-project"
        assert pf.settings.secrets_environment == "production"
        assert pf.settings.secrets_vault == "DevOps"
        assert pf.settings.secrets_file == "secrets.yaml"

    def test_task_secrets_override(self):
        src = """\
task deploy [secrets=sops]:
    deploy it
"""
        pf = parse(src)
        assert pf.tasks["deploy"].options.secrets == "sops"

    def test_set_unknown_raises(self):
        src = """\
set bogus-directive

task build:
    build it
"""
        with pytest.raises(ParseError, match="unknown set directive"):
            parse(src)


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


class TestAliases:
    def test_basic_alias(self):
        src = """\
task deploy:
    deploy it

alias d := deploy
"""
        pf = parse(src)
        assert pf.aliases == {"d": "deploy"}
        assert pf.resolve_alias("d") == "deploy"
        assert pf.resolve_alias("deploy") == "deploy"

    def test_multiple_aliases(self):
        src = """\
task build:
    build it

task deploy:
    deploy it

alias b := build
alias d := deploy
"""
        pf = parse(src)
        assert pf.aliases == {"b": "build", "d": "deploy"}

    def test_alias_unknown_target_raises(self):
        src = """\
alias x := nonexistent

task build:
    build it
"""
        with pytest.raises(ParseError, match="unknown task"):
            parse(src)

    def test_alias_missing_name_raises(self):
        src = """\
task build:
    build it

alias := build
"""
        with pytest.raises(ParseError, match="alias missing name"):
            parse(src)

    def test_alias_missing_target_raises(self):
        src = """\
task build:
    build it

alias x :=
"""
        with pytest.raises(ParseError, match="alias missing target"):
            parse(src)

    def test_alias_bad_syntax_raises(self):
        src = """\
task build:
    build it

alias x = build
"""
        with pytest.raises(ParseError, match="alias must use ':='"):
            parse(src)


# ---------------------------------------------------------------------------
# Backtick variables
# ---------------------------------------------------------------------------


class TestBacktickVariables:
    def test_backtick_variable(self):
        src = """\
version := `echo hello-world`

task show:
    version is {{version}}
"""
        pf = parse(src)
        assert pf.variables["version"] == "hello-world"

    def test_backtick_variable_interpolation(self):
        src = """\
uname := `uname -s`

task show:
    os is {{uname}}
"""
        pf = parse(src)
        # Should be resolved to a non-empty string
        assert len(pf.variables["uname"]) > 0
        resolved = pf.resolve_prompt("show")
        assert "os is" in resolved
        assert "{{uname}}" not in resolved

    def test_backtick_disabled_when_not_allowed(self):
        src = """\
version := `echo hello-world`

task show:
    version is {{version}}
"""
        with pytest.raises(ParseError, match="backtick command substitution is disabled"):
            parse(src, allow_backticks=False)


# ---------------------------------------------------------------------------
# Private, group, doc, confirm, os options
# ---------------------------------------------------------------------------


class TestNewTaskOptions:
    def test_private_option_bare(self):
        src = """\
task internal [private]:
    do secret stuff
"""
        pf = parse(src)
        assert pf.tasks["internal"].options.private is True

    def test_private_option_with_value(self):
        src = """\
task internal [private=true]:
    do secret stuff
"""
        pf = parse(src)
        assert pf.tasks["internal"].options.private is True

    def test_group_option(self):
        src = """\
task build [group=ci]:
    build it
"""
        pf = parse(src)
        assert pf.tasks["build"].options.group == "ci"

    def test_doc_option(self):
        src = """\
task build [doc=Build the project]:
    lots of complex prompt text here
"""
        pf = parse(src)
        assert pf.tasks["build"].options.doc == "Build the project"

    def test_confirm_bare(self):
        src = """\
task deploy [confirm]:
    deploy to production
"""
        pf = parse(src)
        assert pf.tasks["deploy"].options.confirm is True

    def test_confirm_with_message(self):
        src = """\
task deploy [confirm=Are you sure?]:
    deploy to production
"""
        pf = parse(src)
        assert pf.tasks["deploy"].options.confirm == "Are you sure?"

    def test_confirm_function_attribute(self):
        src = """\
task deploy [confirm("Really deploy?")]:
    deploy to production
"""
        pf = parse(src)
        assert pf.tasks["deploy"].options.confirm == "Really deploy?"

    def test_default_attribute_sets_default_task(self):
        src = """\
task first:
    first task

task second [default]:
    second task
"""
        pf = parse(src)
        assert pf.default_task == "second"

    def test_env_function_attribute(self):
        src = """\
task build [env(NODE_ENV, production), env("DEBUG", "0")]:
    !npm run build
"""
        pf = parse(src)
        assert pf.tasks["build"].options.env == {
            "NODE_ENV": "production",
            "DEBUG": "0",
        }

    def test_os_filter(self):
        src = """\
task linuxonly [os=linux]:
    do linux stuff
"""
        pf = parse(src)
        assert pf.tasks["linuxonly"].options.os_filter == "linux"

    def test_working_dir_option(self):
        src = """\
task build [working-dir=/tmp/build]:
    build it
"""
        pf = parse(src)
        assert pf.tasks["build"].options.working_dir == "/tmp/build"

    def test_multiple_new_options(self):
        src = """\
task deploy [group=production, confirm, os=linux, doc=Deploy app]:
    deploy it
"""
        pf = parse(src)
        opts = pf.tasks["deploy"].options
        assert opts.group == "production"
        assert opts.confirm is True
        assert opts.os_filter == "linux"
        assert opts.doc == "Deploy app"

    def test_timeout_options(self):
        src = """\
task slow [timeout=30s, llm-timeout=5m]:
    !sleep 1
    explain the result
"""
        pf = parse(src)
        opts = pf.tasks["slow"].options
        assert opts.timeout == "30s"
        assert opts.llm_timeout == "5m"

    def test_invalid_timeout_raises(self):
        src = """\
task slow [timeout=soon]:
    !sleep 1
"""
        with pytest.raises(ParseError, match="invalid duration"):
            parse(src)

    def test_rollback_option(self):
        src = """\
task rollback:
    undo deploy

task deploy [rollback=rollback]:
    deploy
"""
        pf = parse(src)
        assert pf.tasks["deploy"].options.rollback == "rollback"

    def test_unknown_rollback_raises(self):
        src = """\
task deploy [rollback=missing]:
    deploy
"""
        with pytest.raises(ParseError, match="rollback targets unknown task"):
            parse(src)

    def test_ssh_task_options(self):
        src = """\
hosts web:
    h1

task deploy [on=web, ssh-key=/tmp/id_ed25519, ssh-strict-host-key-checking=accept-new, ssh-parallel]:
    !uptime
"""
        pf = parse(src)
        opts = pf.tasks["deploy"].options
        assert opts.ssh_identity == "/tmp/id_ed25519"
        assert opts.ssh_strict_host_key_checking == "accept-new"
        assert opts.ssh_parallel is True

    def test_private_with_other_options(self):
        src = """\
task secret [private, model=haiku]:
    do secret stuff
"""
        pf = parse(src)
        opts = pf.tasks["secret"].options
        assert opts.private is True
        assert opts.model == "haiku"


# ---------------------------------------------------------------------------
# OS filter — should_skip_for_os
# ---------------------------------------------------------------------------


class TestOsFilter:
    def test_no_filter_never_skips(self):
        from makethlm.models import TaskOptions

        opts = TaskOptions()
        assert opts.should_skip_for_os() is False

    def test_matching_os_does_not_skip(self):
        import platform

        from makethlm.models import TaskOptions

        current = platform.system().lower()
        # Map back to our naming
        reverse_map = {"linux": "linux", "darwin": "macos", "windows": "windows"}
        os_name = reverse_map.get(current, current)
        opts = TaskOptions(os_filter=os_name)
        assert opts.should_skip_for_os() is False

    def test_non_matching_os_skips(self):
        from makethlm.models import TaskOptions

        # Use a platform that definitely won't match
        opts = TaskOptions(os_filter="freebsd")
        assert opts.should_skip_for_os() is True


# ---------------------------------------------------------------------------
# TaskOptions merge
# ---------------------------------------------------------------------------


class TestTaskOptionsMerge:
    def test_merge_basic(self):
        from makethlm.models import TaskOptions

        base = TaskOptions(model="base-model", group="ci")
        override = TaskOptions(model="new-model")
        merged = base.merge(override)
        assert merged.model == "new-model"
        assert merged.group == "ci"

    def test_merge_private(self):
        from makethlm.models import TaskOptions

        base = TaskOptions(private=False)
        override = TaskOptions(private=True)
        merged = base.merge(override)
        assert merged.private is True

    def test_merge_confirm(self):
        from makethlm.models import TaskOptions

        base = TaskOptions(confirm=False)
        override = TaskOptions(confirm="Are you sure?")
        merged = base.merge(override)
        assert merged.confirm == "Are you sure?"


# ---------------------------------------------------------------------------
# Extended set directives (Justfile-compatible)
# ---------------------------------------------------------------------------


class TestExtendedSetDirectives:
    def test_set_export(self):
        pf = parse("""\
set export

project := "myapp"

task build:
    build it
""")
        assert pf.settings.export is True

    def test_set_positional_arguments(self):
        pf = parse("""\
set positional-arguments

task build:
    build it
""")
        assert pf.settings.positional_arguments is True

    def test_set_ignore_comments(self):
        pf = parse("""\
set ignore-comments

task build:
    build it
""")
        assert pf.settings.ignore_comments is True

    def test_set_tempdir(self):
        pf = parse("""\
set tempdir "/tmp/pf"

task build:
    build it
""")
        assert pf.settings.tempdir == "/tmp/pf"

    def test_set_quiet(self):
        pf = parse("""\
set quiet

task build:
    build it
""")
        assert pf.settings.quiet is True

    def test_set_allow_duplicate_tasks(self):
        pf = parse("""\
set allow-duplicate-tasks

task build:
    build v1

task build:
    build v2
""")
        assert pf.settings.allow_duplicate_tasks is True
        # Second definition wins
        assert "v2" in pf.tasks["build"].prompt

    def test_set_dotenv_path(self):
        pf = parse("""\
set dotenv-path ".env.local"

task build:
    build it
""")
        assert pf.settings.dotenv_path == ".env.local"

    def test_set_dotenv_path_implies_dotenv_load(self):
        """Setting dotenv-path should implicitly enable dotenv-load."""
        pf = parse("""\
set dotenv-path ".env.production"

task build:
    build it
""")
        assert pf.settings.dotenv_path == ".env.production"
        assert pf.settings.dotenv_load is True

    def test_set_dotenv_load_with_path(self):
        """dotenv-load accepts a file path to both enable loading and set the path."""
        pf = parse("""\
set dotenv-load ".env.local"

task build:
    build it
""")
        assert pf.settings.dotenv_load is True
        assert pf.settings.dotenv_path == ".env.local"

    def test_set_dotenv_load_with_unquoted_path(self):
        """dotenv-load accepts an unquoted file path."""
        pf = parse("""\
set dotenv-load .env.staging

task build:
    build it
""")
        assert pf.settings.dotenv_load is True
        assert pf.settings.dotenv_path == ".env.staging"

    def test_set_dotenv_load_with_path_directory(self):
        """dotenv-load accepts a path with directories."""
        pf = parse("""\
set dotenv-load "config/.env"

task build:
    build it
""")
        assert pf.settings.dotenv_load is True
        assert pf.settings.dotenv_path == "config/.env"

    def test_set_dotenv_load_true_does_not_set_path(self):
        """dotenv-load with explicit 'true' should not set dotenv_path."""
        pf = parse("""\
set dotenv-load true

task build:
    build it
""")
        assert pf.settings.dotenv_load is True
        assert pf.settings.dotenv_path is None

    def test_set_dotenv_required(self):
        pf = parse("""\
set dotenv-required

task build:
    build it
""")
        assert pf.settings.dotenv_required is True


class TestSetDirectiveVariableResolution:
    """Set directive string values support the same expressions as variables."""

    def test_set_directive_uses_variable_concat(self):
        """String directive values can reference variables declared above via +."""
        pf = parse("""\
project_dir := "/opt/myapp"
set working-dir project_dir + "/src"

task build:
    build it
""")
        assert pf.settings.working_dir == "/opt/myapp/src"

    def test_set_dotenv_load_uses_variable_concat(self):
        """dotenv-load path supports variable concatenation."""
        pf = parse("""\
config_dir := "/etc/myapp"
set dotenv-load config_dir + "/.env"

task build:
    build it
""")
        assert pf.settings.dotenv_load is True
        assert pf.settings.dotenv_path == "/etc/myapp/.env"

    def test_set_dotenv_path_uses_variable_concat(self):
        """dotenv-path supports variable concatenation."""
        pf = parse("""\
base := "/opt"
set dotenv-path base + "/config/.env"

task build:
    build it
""")
        assert pf.settings.dotenv_path == "/opt/config/.env"
        assert pf.settings.dotenv_load is True

    def test_set_directive_if_else_expression(self):
        """String directive values support if/else expressions."""
        pf = parse("""\
env := "prod"
set working-dir if env == "prod" { "/opt/app" } else { "/tmp/app" }

task build:
    build it
""")
        assert pf.settings.working_dir == "/opt/app"

    def test_set_directive_multi_variable_concat(self):
        """String directive values support multi-part concatenation."""
        pf = parse("""\
home := "/home/deploy"
project := "myapp"
set working-dir home + "/" + project

task build:
    build it
""")
        assert pf.settings.working_dir == "/home/deploy/myapp"

    def test_set_directive_bare_value_still_works(self):
        """Bare unquoted values still work (e.g. set sandbox docker)."""
        parse("""\
task build:
    build it
""")
        # Verify existing bare-value directives still parse
        pf2 = parse("""\
set sandbox docker

task build:
    build it
""")
        assert pf2.settings.sandbox == "docker"

    def test_set_directive_quoted_string_still_works(self):
        """Plain quoted strings still work as before."""
        pf = parse("""\
set shell "/bin/bash"

task build:
    build it
""")
        assert pf.settings.shell == "/bin/bash"


# ---------------------------------------------------------------------------
# Export variables
# ---------------------------------------------------------------------------


class TestExportVariables:
    def test_export_variable(self):
        pf = parse("""\
export API_KEY := "secret123"

task build:
    build with {{API_KEY}}
""")
        assert pf.variables["API_KEY"] == "secret123"
        assert "API_KEY" in pf.exported_vars

    def test_export_bare_marks_exported(self):
        pf = parse("""\
MY_VAR := "value"
export MY_VAR

task build:
    build it
""")
        assert "MY_VAR" in pf.exported_vars

    def test_export_with_set_export(self):
        pf = parse("""\
set export

project := "myapp"
version := "1.0"

task build:
    build it
""")
        exported = pf.get_exported_env()
        assert exported == {"project": "myapp", "version": "1.0"}

    def test_export_only_marked(self):
        pf = parse("""\
project := "myapp"
export secret := "key123"

task build:
    build it
""")
        exported = pf.get_exported_env()
        assert "secret" in exported
        assert "project" not in exported


# ---------------------------------------------------------------------------
# String concatenation
# ---------------------------------------------------------------------------


class TestStringConcatenation:
    def test_concat_quoted_strings(self):
        pf = parse("""\
full := "hello" + "-" + "world"

task show:
    value is {{full}}
""")
        assert pf.variables["full"] == "hello-world"

    def test_concat_with_variable(self):
        pf = parse("""\
name := "app"
full := "my-" + name + "-v1"

task show:
    value is {{full}}
""")
        assert pf.variables["full"] == "my-app-v1"


# ---------------------------------------------------------------------------
# If/else expressions
# ---------------------------------------------------------------------------


class TestIfElseExpressions:
    def test_if_else_in_variable(self):
        pf = parse("""\
mode := "production"
greeting := if mode == "production" { "deploy carefully" } else { "test freely" }

task show:
    {{greeting}}
""")
        assert pf.variables["greeting"] == "deploy carefully"

    def test_if_else_not_equal(self):
        pf = parse("""\
mode := "dev"
msg := if mode != "production" { "testing" } else { "deploying" }

task show:
    {{msg}}
""")
        assert pf.variables["msg"] == "testing"

    def test_if_else_in_template(self):
        pf = parse("""\
env := "prod"

task show:
    {{if env == "prod" { "production mode" } else { "dev mode" }}}
""")
        resolved = pf.resolve_prompt("show")
        assert "production mode" in resolved

    def test_if_else_else_branch(self):
        pf = parse("""\
mode := "dev"
msg := if mode == "production" { "careful" } else { "fast" }

task show:
    {{msg}}
""")
        assert pf.variables["msg"] == "fast"


# ---------------------------------------------------------------------------
# Built-in functions
# ---------------------------------------------------------------------------


class TestBuiltinFunctions:
    def test_os_function(self):
        import platform

        pf = parse("""\
task show:
    running on {{os()}}
""")
        resolved = pf.resolve_prompt("show")
        expected_os = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(
            platform.system().lower(), platform.system().lower()
        )
        assert expected_os in resolved

    def test_arch_function(self):
        import platform

        pf = parse("""\
task show:
    arch is {{arch()}}
""")
        resolved = pf.resolve_prompt("show")
        assert platform.machine() in resolved

    def test_num_cpus_function(self):
        import os

        pf = parse("""\
task show:
    cpus: {{num_cpus()}}
""")
        resolved = pf.resolve_prompt("show")
        assert str(os.cpu_count()) in resolved

    def test_home_directory_function(self):
        pf = parse("""\
task show:
    home: {{home_directory()}}
""")
        resolved = pf.resolve_prompt("show")
        assert os.path.expanduser("~") in resolved


# ---------------------------------------------------------------------------
# String functions
# ---------------------------------------------------------------------------


class TestStringFunctions:
    def test_uppercase(self):
        pf = parse("""\
name := "hello"

task show:
    {{uppercase(name)}}
""")
        assert pf.resolve_prompt("show") == "HELLO"

    def test_lowercase(self):
        pf = parse("""\
name := "HELLO"

task show:
    {{lowercase(name)}}
""")
        assert pf.resolve_prompt("show") == "hello"

    def test_trim(self):
        pf = parse("""\
name := "  hello  "

task show:
    {{trim(name)}}
""")
        assert pf.resolve_prompt("show") == "hello"

    def test_trim_start(self):
        pf = parse("""\
name := "  hello  "

task show:
    {{trim_start(name)}}
""")
        assert pf.resolve_prompt("show") == "hello  "

    def test_trim_end(self):
        pf = parse("""\
name := "  hello  "

task show:
    {{trim_end(name)}}
""")
        assert pf.resolve_prompt("show") == "  hello"

    def test_replace(self):
        pf = parse("""\
name := "hello world"

task show:
    {{replace(name, "world", "there")}}
""")
        assert pf.resolve_prompt("show") == "hello there"

    def test_replace_regex(self):
        pf = parse("""\
name := "hello123world"

task show:
    {{replace_regex(name, "[0-9]+", "-")}}
""")
        assert pf.resolve_prompt("show") == "hello-world"

    def test_quote(self):
        pf = parse("""\
name := "hello world"

task show:
    {{quote(name)}}
""")
        resolved = pf.resolve_prompt("show")
        assert resolved == "'hello world'"

    def test_join(self):
        pf = parse("""\
a := "one"
b := "two"
c := "three"

task show:
    {{join("-", a, b, c)}}
""")
        assert pf.resolve_prompt("show") == "one-two-three"

    def test_file_name(self):
        pf = parse("""\
path := "/home/user/file.txt"

task show:
    {{file_name(path)}}
""")
        assert pf.resolve_prompt("show") == "file.txt"

    def test_file_stem(self):
        pf = parse("""\
path := "/home/user/file.txt"

task show:
    {{file_stem(path)}}
""")
        assert pf.resolve_prompt("show") == "file"

    def test_parent_directory(self):
        pf = parse("""\
path := "/home/user/file.txt"

task show:
    {{parent_directory(path)}}
""")
        assert pf.resolve_prompt("show") == "/home/user"

    def test_extension(self):
        pf = parse("""\
path := "/home/user/file.txt"

task show:
    {{extension(path)}}
""")
        assert pf.resolve_prompt("show") == "txt"

    def test_without_extension(self):
        pf = parse("""\
path := "/home/user/file.txt"

task show:
    {{without_extension(path)}}
""")
        assert pf.resolve_prompt("show") == "/home/user/file"

    def test_contains_true(self):
        pf = parse("""\
name := "hello world"

task show:
    {{contains(name, "world")}}
""")
        assert pf.resolve_prompt("show") == "true"

    def test_contains_false(self):
        pf = parse("""\
name := "hello world"

task show:
    {{contains(name, "xyz")}}
""")
        assert pf.resolve_prompt("show") == "false"

    def test_starts_with(self):
        pf = parse("""\
name := "hello world"

task show:
    {{starts_with(name, "hello")}}
""")
        assert pf.resolve_prompt("show") == "true"

    def test_ends_with(self):
        pf = parse("""\
name := "hello world"

task show:
    {{ends_with(name, "world")}}
""")
        assert pf.resolve_prompt("show") == "true"

    def test_len(self):
        pf = parse("""\
name := "hello"

task show:
    {{len(name)}}
""")
        assert pf.resolve_prompt("show") == "5"

    def test_substr_with_start(self):
        pf = parse("""\
name := "hello world"

task show:
    {{substr(name, "6")}}
""")
        assert pf.resolve_prompt("show") == "world"

    def test_substr_with_start_and_end(self):
        pf = parse("""\
name := "hello world"

task show:
    {{substr(name, "0", "5")}}
""")
        assert pf.resolve_prompt("show") == "hello"

    def test_match_true(self):
        pf = parse("""\
name := "file.tar.gz"

task show:
    {{match(name, ".*\\.gz$")}}
""")
        assert pf.resolve_prompt("show") == "true"

    def test_match_false(self):
        pf = parse("""\
name := "file.tar.gz"

task show:
    {{match(name, ".*\\.zip$")}}
""")
        assert pf.resolve_prompt("show") == "false"

    def test_nested_function_calls(self):
        pf = parse("""\
name := "  Hello World  "

task show:
    {{uppercase(trim(name))}}
""")
        assert pf.resolve_prompt("show") == "HELLO WORLD"

    def test_function_with_literal_args(self):
        pf = parse("""\
task show:
    {{replace("hello world", "world", "there")}}
""")
        assert pf.resolve_prompt("show") == "hello there"

    def test_function_in_shell_step(self):
        pf = parse("""\
name := "hello"

task show:
    !echo {{uppercase(name)}}
""")
        steps = pf.resolve_steps("show")
        assert steps[0].kind == "shell"
        assert "HELLO" in steps[0].content


# ---------------------------------------------------------------------------
# Bash-style parameter expansion
# ---------------------------------------------------------------------------


class TestParameterExpansion:
    def test_hash_shortest_prefix(self):
        pf = parse("""\
name := "hello-world"

task show:
    {{name#*-}}
""")
        assert pf.resolve_prompt("show") == "world"

    def test_double_hash_longest_prefix(self):
        pf = parse("""\
name := "a-b-c"

task show:
    {{name##*-}}
""")
        assert pf.resolve_prompt("show") == "c"

    def test_percent_shortest_suffix(self):
        pf = parse("""\
name := "hello-world.tar.gz"

task show:
    {{name%.*}}
""")
        assert pf.resolve_prompt("show") == "hello-world.tar"

    def test_double_percent_longest_suffix(self):
        pf = parse("""\
name := "hello-world.tar.gz"

task show:
    {{name%%.*}}
""")
        assert pf.resolve_prompt("show") == "hello-world"

    def test_slash_replace_first(self):
        pf = parse("""\
name := "hello-hello-world"

task show:
    {{name/hello/goodbye}}
""")
        assert pf.resolve_prompt("show") == "goodbye-hello-world"

    def test_double_slash_replace_all(self):
        pf = parse("""\
name := "hello-hello-world"

task show:
    {{name//hello/goodbye}}
""")
        assert pf.resolve_prompt("show") == "goodbye-goodbye-world"


# ---------------------------------------------------------------------------
# Version functions and v"..." syntax
# ---------------------------------------------------------------------------


class TestVersionFunctions:
    def test_version_literal(self):
        pf = parse("""\
version := v"1.2.3"

task show:
    {{version}}
""")
        assert pf.variables["version"] == "1.2.3"
        assert pf.resolve_prompt("show") == "1.2.3"

    def test_version_literal_single_quotes(self):
        pf = parse("""\
version := v'1.2.3'

task show:
    {{version}}
""")
        assert pf.variables["version"] == "1.2.3"

    def test_version_major(self):
        pf = parse("""\
version := "1.2.3"

task show:
    {{version_major(version)}}
""")
        assert pf.resolve_prompt("show") == "1"

    def test_version_minor(self):
        pf = parse("""\
version := "1.2.3"

task show:
    {{version_minor(version)}}
""")
        assert pf.resolve_prompt("show") == "2"

    def test_version_patch(self):
        pf = parse("""\
version := "1.2.3"

task show:
    {{version_patch(version)}}
""")
        assert pf.resolve_prompt("show") == "3"

    def test_bump_patch(self):
        pf = parse("""\
version := "1.2.3"

task show:
    {{bump_patch(version)}}
""")
        assert pf.resolve_prompt("show") == "1.2.4"

    def test_bump_minor(self):
        pf = parse("""\
version := "1.2.3"

task show:
    {{bump_minor(version)}}
""")
        assert pf.resolve_prompt("show") == "1.3.0"

    def test_bump_major(self):
        pf = parse("""\
version := "1.2.3"

task show:
    {{bump_major(version)}}
""")
        assert pf.resolve_prompt("show") == "2.0.0"

    def test_version_comparison_ge(self):
        pf = parse("""\
version := "2.1.0"
result := if version >= "2.0.0" { "v2+" } else { "v1" }

task show:
    {{result}}
""")
        assert pf.variables["result"] == "v2+"

    def test_version_comparison_lt(self):
        pf = parse("""\
version := "1.9.9"
result := if version < "2.0.0" { "v1" } else { "v2+" }

task show:
    {{result}}
""")
        assert pf.variables["result"] == "v1"

    def test_version_comparison_gt(self):
        pf = parse("""\
version := "3.0.0"
result := if version > "2.0.0" { "newer" } else { "older" }

task show:
    {{result}}
""")
        assert pf.variables["result"] == "newer"

    def test_version_comparison_le(self):
        pf = parse("""\
version := "2.0.0"
result := if version <= "2.0.0" { "ok" } else { "too new" }

task show:
    {{result}}
""")
        assert pf.variables["result"] == "ok"

    def test_version_from_command(self):
        pf = parse("""\
version := v`echo 1.0.0`

task show:
    {{version}}
""")
        assert pf.variables["version"] == "1.0.0"

    def test_numeric_comparison(self):
        pf = parse("""\
count := "5"
result := if count > "3" { "many" } else { "few" }

task show:
    {{result}}
""")
        assert pf.variables["result"] == "many"


# ---------------------------------------------------------------------------
# Default environment variables (makethlm_task, makethlm_file, makethlm_dir)
# ---------------------------------------------------------------------------


class TestDefaultEnvVars:
    def test_makethlm_task_variable(self):
        pf = parse("""\
task build:
    running {{makethlm_task}}
""")
        resolved = pf.resolve_prompt("build")
        assert resolved == "running build"

    def test_makethlm_task_in_different_tasks(self):
        pf = parse("""\
task alpha:
    task is {{makethlm_task}}

task beta:
    task is {{makethlm_task}}
""")
        assert pf.resolve_prompt("alpha") == "task is alpha"
        assert pf.resolve_prompt("beta") == "task is beta"

    def test_makethlm_file_with_path(self):
        pf = parse("""\
task show:
    file: {{makethlm_file}}
""")
        resolved = pf.resolve_prompt("show", promptfile_path="/tmp/my/Promptfile")
        assert resolved == "file: /tmp/my/Promptfile"

    def test_makethlm_dir_with_path(self):
        pf = parse("""\
task show:
    dir: {{makethlm_dir}}
""")
        resolved = pf.resolve_prompt("show", promptfile_path="/tmp/my/Promptfile")
        assert "/tmp/my" in resolved

    def test_makethlm_vars_in_shell_steps(self):
        pf = parse("""\
task build:
    !echo {{makethlm_task}}
""")
        steps = pf.resolve_steps("build")
        assert steps[0].content == "echo build"


# ---------------------------------------------------------------------------
# Variadic arguments
# ---------------------------------------------------------------------------


class TestVariadicArguments:
    def test_plus_variadic(self):
        pf = parse("""\
task greet(+names):
    say hello to {{names}}
""")
        assert pf.tasks["greet"].arguments[0].variadic == "+"
        assert pf.tasks["greet"].arguments[0].name == "names"

    def test_star_variadic(self):
        pf = parse("""\
task greet(*names):
    say hello to {{names}}
""")
        assert pf.tasks["greet"].arguments[0].variadic == "*"
        assert pf.tasks["greet"].arguments[0].name == "names"

    def test_mixed_args_and_variadic(self):
        pf = parse("""\
task deploy(target, +files):
    deploy {{files}} to {{target}}
""")
        assert pf.tasks["deploy"].arguments[0].name == "target"
        assert pf.tasks["deploy"].arguments[0].variadic is None
        assert pf.tasks["deploy"].arguments[1].name == "files"
        assert pf.tasks["deploy"].arguments[1].variadic == "+"


# ---------------------------------------------------------------------------
# Bare OS attributes
# ---------------------------------------------------------------------------


class TestBareOsAttributes:
    def test_linux_attribute(self):
        pf = parse("""\
task build [linux]:
    linux only
""")
        assert pf.tasks["build"].options.os_filter == "linux"

    def test_macos_attribute(self):
        pf = parse("""\
task build [macos]:
    macos only
""")
        assert pf.tasks["build"].options.os_filter == "macos"

    def test_unix_attribute(self):
        pf = parse("""\
task build [unix]:
    unix only
""")
        assert pf.tasks["build"].options.os_filter == "unix"

    def test_unix_matches_linux(self):
        import platform

        from makethlm.models import TaskOptions

        opts = TaskOptions(os_filter="unix")
        if platform.system().lower() in ("linux", "darwin"):
            assert opts.should_skip_for_os() is False
        else:
            assert opts.should_skip_for_os() is True


# ---------------------------------------------------------------------------
# no-cd, no-exit-message, no-quiet attributes
# ---------------------------------------------------------------------------


class TestNewAttributes:
    def test_no_cd(self):
        pf = parse("""\
task build [no-cd]:
    build it
""")
        assert pf.tasks["build"].options.no_cd is True

    def test_no_exit_message(self):
        pf = parse("""\
task build [no-exit-message]:
    build it
""")
        assert pf.tasks["build"].options.no_exit_message is True

    def test_no_quiet(self):
        pf = parse("""\
task build [no-quiet]:
    build it
""")
        assert pf.tasks["build"].options.no_quiet is True

    def test_positional_arguments_option(self):
        pf = parse("""\
task build [positional-arguments]:
    build it
""")
        assert pf.tasks["build"].options.positional_arguments is True


# ---------------------------------------------------------------------------
# Underscore private convention
# ---------------------------------------------------------------------------


class TestUnderscorePrivate:
    def test_underscore_prefix_is_private(self):
        pf = parse("""\
task _helper:
    internal stuff

task build:
    build it
""")
        assert pf.tasks["_helper"].options.private is True
        assert pf.tasks["build"].options.private is False


# ---------------------------------------------------------------------------
# Line continuation
# ---------------------------------------------------------------------------


class TestLineContinuation:
    def test_backslash_continuation(self):
        pf = parse("""\
task build:
    this is a long \\
    prompt that continues
""")
        assert "long prompt that continues" in pf.tasks["build"].prompt

    def test_variable_continuation(self):
        pf = parse("""\
greeting := "hello" + \\
    " world"

task show:
    {{greeting}}
""")
        assert pf.variables["greeting"] == "hello world"


# ---------------------------------------------------------------------------
# Single-quoted strings
# ---------------------------------------------------------------------------


class TestSingleQuotedStrings:
    def test_single_quoted_variable(self):
        pf = parse("""\
name := 'hello world'

task show:
    {{name}}
""")
        assert pf.variables["name"] == "hello world"


# ---------------------------------------------------------------------------
# Quiet prefix
# ---------------------------------------------------------------------------


class TestQuietPrefix:
    def test_at_quiet_prefix(self):
        pf = parse("""\
task build:
    !@echo hello
""")
        step = pf.tasks["build"].steps[0]
        assert step.quiet is True
        assert step.content == "echo hello"


# ---------------------------------------------------------------------------
# Example Promptfiles — ensure all shipped examples parse without errors
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class TestAgents:
    def test_basic_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "security.md")
            with open(md_path, "w") as f:
                f.write("You are a security expert.\nFocus on vulnerabilities.\n")

            main_src = """\
agent security "./security.md"

task audit [agent=security]:
    Review the latest git diff.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert "security" in pf.agents
            assert (
                pf.agents["security"].instructions
                == "You are a security expert.\nFocus on vulnerabilities."
            )
            assert pf.tasks["audit"].options.agent == "security"

    def test_agent_with_llm_and_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "reviewer.md")
            with open(md_path, "w") as f:
                f.write("You review code.\n")

            main_src = """\
llm claude [model=opus]

agent reviewer "./reviewer.md" [llm=claude, model=sonnet]

task review [agent=reviewer]:
    Review this PR.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            agent = pf.agents["reviewer"]
            assert agent.llm == "claude"
            assert agent.model == "sonnet"

    def test_agent_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_src = """\
agent missing "./nonexistent.md"

task t [agent=missing]:
    do stuff
"""
            with pytest.raises(ParseError, match="agent file not found"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_duplicate_agent_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "a.md")
            with open(md_path, "w") as f:
                f.write("Agent A\n")

            main_src = """\
agent myagent "./a.md"
agent myagent "./a.md"

task t [agent=myagent]:
    do stuff
"""
            with pytest.raises(ParseError, match="duplicate agent"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_unknown_agent_reference_raises(self):
        src = """\
task bad [agent=nonexistent]:
    do stuff
"""
        with pytest.raises(ParseError, match="unknown agent"):
            parse(src)

    def test_agent_prepends_to_prompt_steps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "security.md")
            with open(md_path, "w") as f:
                f.write("You are a security expert.\n")

            main_src = """\
agent security "./security.md"

task audit [agent=security]:
    Review the code for vulnerabilities.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            steps = pf.resolve_steps("audit")
            assert len(steps) == 1
            assert steps[0].kind == "prompt"
            assert steps[0].content.startswith("You are a security expert.")
            assert "Review the code for vulnerabilities." in steps[0].content

    def test_agent_does_not_prepend_to_shell_steps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("Agent instructions.\n")

            main_src = """\
agent myagent "./agent.md"

task mixed [agent=myagent]:
    !echo hello
    Review the output.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            steps = pf.resolve_steps("mixed")
            assert len(steps) == 2
            # Shell step should NOT have agent instructions
            assert steps[0].kind == "shell"
            assert "Agent instructions" not in steps[0].content
            # Prompt step should have agent instructions
            assert steps[1].kind == "prompt"
            assert steps[1].content.startswith("Agent instructions.")

    def test_agent_included_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("Included agent.\n")

            lib = os.path.join(tmpdir, "lib.pf")
            with open(lib, "w") as f:
                f.write('agent shared "./agent.md"\n')

            main_src = """\
include "lib.pf"

task t [agent=shared]:
    do stuff
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert "shared" in pf.agents

    def test_agent_unquoted_path_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_src = """\
agent bad ./unquoted.md

task t [agent=bad]:
    do stuff
"""
            with pytest.raises(ParseError, match="agent path must be quoted"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_agent_missing_path_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_src = """\
agent nopath

task t:
    do stuff
"""
            with pytest.raises(ParseError, match="agent requires a name and a quoted path"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_agent_unknown_option_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("Agent.\n")

            main_src = """\
agent myagent "./agent.md" [bogus=123]

task t [agent=myagent]:
    do stuff
"""
            with pytest.raises(ParseError, match="unknown agent option"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))


# ---------------------------------------------------------------------------
# Ansible Inventory directive
# ---------------------------------------------------------------------------


class TestInventoryDirective:
    def test_basic_inventory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = os.path.join(tmpdir, "hosts.ini")
            with open(inv_path, "w") as f:
                f.write("[webservers]\nweb1.example.com\nweb2.example.com\n")

            main_src = """\
inventory "./hosts.ini"

task deploy [on=webservers]:
    !systemctl restart myapp
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert "webservers" in pf.host_groups
            assert pf.host_groups["webservers"].hosts == ["web1.example.com", "web2.example.com"]

    def test_inventory_with_vars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = os.path.join(tmpdir, "hosts.ini")
            with open(inv_path, "w") as f:
                f.write("[databases]\ndb1.example.com ansible_user=postgres ansible_port=5432\n")

            main_src = """\
inventory "./hosts.ini"

task backup [on=databases]:
    !pg_dump mydb
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            grp = pf.host_groups["databases"]
            connection = grp.connections["db1.example.com"]
            assert connection.user == "postgres"
            assert connection.port == 5432

    def test_inventory_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_src = """\
inventory "./nonexistent.ini"

task t:
    do stuff
"""
            with pytest.raises(ParseError, match="inventory file not found"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_inventory_conflict_with_hosts_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = os.path.join(tmpdir, "hosts.ini")
            with open(inv_path, "w") as f:
                f.write("[web]\nweb1\n")

            main_src = """\
hosts web:
    manual-host

inventory "./hosts.ini"

task t [on=web]:
    !uptime
"""
            with pytest.raises(ParseError, match="conflicts with existing host group"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_inventory_unquoted_path_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_src = """\
inventory ./unquoted.ini

task t:
    do stuff
"""
            with pytest.raises(ParseError, match="inventory path must be quoted"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_inventory_coexists_with_hosts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = os.path.join(tmpdir, "hosts.ini")
            with open(inv_path, "w") as f:
                f.write("[databases]\ndb1\n")

            main_src = """\
hosts web:
    web1

inventory "./hosts.ini"

task deploy_web [on=web]:
    !deploy.sh

task deploy_db [on=databases]:
    !pg_dump
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert "web" in pf.host_groups
            assert "databases" in pf.host_groups

    def test_inventory_multiple_groups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = os.path.join(tmpdir, "hosts.ini")
            with open(inv_path, "w") as f:
                f.write("[webservers]\nweb1\nweb2\n\n[databases]\ndb1\n")

            main_src = """\
inventory "./hosts.ini"

task deploy_web [on=webservers]:
    !deploy.sh

task deploy_db [on=databases]:
    !backup.sh
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert len(pf.host_groups) == 2


# ---------------------------------------------------------------------------
# Guidance directive
# ---------------------------------------------------------------------------


class TestGuidance:
    def test_inline_guidance(self):
        src = """\
guidance:
    Always be concise.
    Never use jargon.

task build:
    build it
"""
        pf = parse(src)
        assert pf.guidance == "Always be concise.\nNever use jargon."

    def test_guidance_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rules_path = os.path.join(tmpdir, "rules.md")
            with open(rules_path, "w") as f:
                f.write("# Rules\n- Be helpful\n- Be safe\n")

            main_src = """\
guidance "./rules.md"

task build:
    build it
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert pf.guidance == "# Rules\n- Be helpful\n- Be safe"

    def test_guidance_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_src = """\
guidance "./nonexistent.md"

task build:
    build it
"""
            with pytest.raises(ParseError, match="guidance file not found"):
                parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

    def test_guidance_empty_block_raises(self):
        src = """\
guidance:

task build:
    build it
"""
        with pytest.raises(ParseError, match="guidance block has no body"):
            parse(src)

    def test_guidance_prepends_to_prompt_steps(self):
        src = """\
guidance:
    Always be concise.

task build:
    build the project
"""
        pf = parse(src)
        steps = pf.resolve_steps("build")
        assert len(steps) == 1
        assert steps[0].content.startswith("Always be concise.")
        assert "build the project" in steps[0].content

    def test_guidance_not_prepended_to_shell_steps(self):
        src = """\
guidance:
    Always be concise.

task build:
    !echo hello
    build the project
"""
        pf = parse(src)
        steps = pf.resolve_steps("build")
        assert steps[0].kind == "shell"
        assert "Always be concise" not in steps[0].content
        assert steps[1].kind == "prompt"
        assert steps[1].content.startswith("Always be concise.")

    def test_guidance_with_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "security.md")
            with open(md_path, "w") as f:
                f.write("You are a security expert.\n")

            main_src = """\
guidance:
    Follow all safety rules.

agent security "./security.md"

task audit [agent=security]:
    Review the code.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            steps = pf.resolve_steps("audit")
            assert len(steps) == 1
            content = steps[0].content
            # Guidance comes first, then agent instructions, then task content
            guidance_pos = content.index("Follow all safety rules.")
            agent_pos = content.index("You are a security expert.")
            task_pos = content.index("Review the code.")
            assert guidance_pos < agent_pos < task_pos

    def test_guidance_included_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib = os.path.join(tmpdir, "lib.pf")
            with open(lib, "w") as f:
                f.write("guidance:\n    Included guidance.\n")

            main_src = """\
include "lib.pf"

task build:
    build it
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert pf.guidance == "Included guidance."

    def test_local_guidance_overrides_included(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib = os.path.join(tmpdir, "lib.pf")
            with open(lib, "w") as f:
                f.write("guidance:\n    Included guidance.\n")

            main_src = """\
guidance:
    Local guidance.

include "lib.pf"

task build:
    build it
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            assert pf.guidance == "Local guidance."

    def test_guidance_bad_syntax_raises(self):
        src = """\
guidance

task build:
    build it
"""
        with pytest.raises(ParseError, match="guidance must be followed"):
            parse(src)


# ---------------------------------------------------------------------------
# Sandbox options
# ---------------------------------------------------------------------------


class TestSandboxOptions:
    def test_sandbox_docker_option(self):
        pf = parse("""\
task build [sandbox=docker]:
    !make all
""")
        assert pf.tasks["build"].options.sandbox == "docker"

    def test_sandbox_systemd_option(self):
        pf = parse("""\
task compile [sandbox=systemd]:
    !gcc -o output main.c
""")
        assert pf.tasks["compile"].options.sandbox == "systemd"

    def test_sandbox_bwrap_option(self):
        pf = parse("""\
task risky [sandbox=bwrap]:
    !./untrusted
""")
        assert pf.tasks["risky"].options.sandbox == "bwrap"

    def test_sandbox_none_option(self):
        pf = parse("""\
task safe [sandbox=none]:
    !echo safe
""")
        assert pf.tasks["safe"].options.sandbox == "none"

    def test_sandbox_invalid_raises(self):
        with pytest.raises(ParseError, match="sandbox must be"):
            parse("""\
task t [sandbox=chroot]:
    !echo nope
""")

    def test_sandbox_with_image(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-image=node:20-alpine]:
    !npm run build
""")
        assert pf.tasks["build"].options.sandbox == "docker"
        assert pf.tasks["build"].options.sandbox_image == "node:20-alpine"

    def test_sandbox_with_mount(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-mount=./dist:/app/dist]:
    !deploy.sh
""")
        assert pf.tasks["build"].options.sandbox_mount == "./dist:/app/dist"

    def test_sandbox_with_net(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-net=host]:
    !deploy.sh
""")
        assert pf.tasks["build"].options.sandbox_net == "host"

    def test_invalid_sandbox_network_is_rejected(self):
        with pytest.raises(ParseError, match="sandbox_net must be"):
            parse("""\
task build [sandbox=docker, sandbox-net=noen]:
    !make all
""")

    def test_sandbox_read_only(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-read-only]:
    !make all
""")
        assert pf.tasks["build"].options.sandbox_read_only is True

    def test_set_sandbox_global(self):
        pf = parse("""\
set sandbox docker

task build:
    !make all
""")
        assert pf.settings.sandbox == "docker"

    def test_sandbox_underscore_forms(self):
        pf = parse("""\
task build [sandbox=docker, sandbox_image=python:3.11, sandbox_mount=./src:/app, sandbox_net=none, sandbox_read_only]:
    !python build.py
""")
        assert pf.tasks["build"].options.sandbox_image == "python:3.11"
        assert pf.tasks["build"].options.sandbox_mount == "./src:/app"
        assert pf.tasks["build"].options.sandbox_net == "none"
        assert pf.tasks["build"].options.sandbox_read_only is True


class TestExamplesCompile:
    """Validate that every example Promptfile (and the root one) can be parsed.

    This is a compile-time check only — no tasks are executed, no LLM calls
    are made, and no libraries are generated.
    """

    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _parse_promptfile(self, path: str):
        """Helper: read and parse a Promptfile, returning the parsed model."""
        abs_path = os.path.join(self._PROJECT_ROOT, path)
        with open(abs_path) as f:
            source = f.read()
        return parse(source, filename=abs_path)

    # -- Individual example tests --

    def test_root_promptfile_compiles(self):
        pf = self._parse_promptfile("Promptfile")
        assert len(pf.tasks) > 0

    def test_python_project_compiles(self):
        pf = self._parse_promptfile("examples/python-project/Promptfile")
        assert "test" in pf.tasks
        assert "document" in pf.tasks
        assert "review" in pf.tasks

    def test_npm_project_compiles(self):
        pf = self._parse_promptfile("examples/npm-project/Promptfile")
        assert "setup" in pf.tasks
        assert "build" in pf.tasks
        assert "test" in pf.tasks

    def test_c_project_compiles(self):
        pf = self._parse_promptfile("examples/c-project/Promptfile")
        assert "generate-lib" in pf.tasks
        assert "build" in pf.tasks
        assert "run" in pf.tasks
        # build should NOT depend on generate-lib (no LLM call on every build)
        assert "generate-lib" not in pf.tasks["build"].dependencies

    def test_blog_generator_compiles(self):
        pf = self._parse_promptfile("examples/blog-generator/Promptfile")
        assert "draft" in pf.tasks
        assert "summarize" in pf.tasks
        assert "tweet-thread" in pf.tasks
        # Verify the function reference works
        assert "writing-style" in pf.functions

    # -- Catch-all: discover and parse every Promptfile under examples/ --

    def test_all_example_promptfiles_compile(self):
        """Walk examples/ and parse every Promptfile found.

        This catches new examples that are added without a dedicated test.
        """
        examples_dir = os.path.join(self._PROJECT_ROOT, "examples")
        found = []
        for root, _dirs, files in os.walk(examples_dir):
            for fname in files:
                if fname in ("Promptfile", "promptfile", "Promptfile.pf", "promptfile.pf"):
                    found.append(os.path.join(root, fname))
        assert found, "no example Promptfiles found — directory structure may have changed"
        for pf_path in found:
            with open(pf_path) as f:
                source = f.read()
            pf = parse(source, filename=pf_path)
            assert len(pf.tasks) > 0, f"{pf_path} parsed but has no tasks"


class TestParserHardening:
    def test_quoted_plus_is_literal(self):
        pf = parse("""\
url := "https://example.test/search?q=a+b"

task show:
    {{url}}
""")
        assert pf.variables["url"] == "https://example.test/search?q=a+b"

    def test_failed_backtick_is_parse_error(self):
        with pytest.raises(ParseError, match="status 7"):
            parse("""\
value := `exit 7`

task show:
    {{value}}
""")

    def test_failed_backtick_in_set_is_not_downgraded_to_literal(self):
        with pytest.raises(ParseError, match="status 9"):
            parse("""\
set working-dir `exit 9`

task show:
    !pwd
""")

    def test_duplicate_variables_require_opt_in(self):
        with pytest.raises(ParseError, match="duplicate variable"):
            parse("""\
value := "one"
value := "two"

task show:
    {{value}}
""")

        pf = parse("""\
set allow-duplicate-variables
value := "one"
value := "two"

task show:
    {{value}}
""")
        assert pf.variables["value"] == "two"

    def test_diamond_includes_are_not_reported_as_cycles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            common = os.path.join(tmpdir, "common.pf")
            left = os.path.join(tmpdir, "left.pf")
            right = os.path.join(tmpdir, "right.pf")
            with open(common, "w") as f:
                f.write("task common:\n    shared\n")
            with open(left, "w") as f:
                f.write('include "common.pf"\n')
            with open(right, "w") as f:
                f.write('include "common.pf"\n')

            pf = parse(
                'include "left.pf"\ninclude "right.pf"\n',
                filename=os.path.join(tmpdir, "Promptfile"),
            )

        assert list(pf.tasks) == ["common"]

    def test_module_resources_remain_namespaced_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            module_path = os.path.join(tmpdir, "ops.pf")
            with open(module_path, "w") as f:
                f.write("""\
service := "payments"
llm openai [model=gpt-test]
hosts web [user=deploy]:
    web.example.test
fn checklist:
    inspect {{service}}
guidance:
    Follow module policy.
task undo:
    !echo undo
task deploy [llm=openai, on=web, rollback=undo]:
    @use checklist
""")

            pf = parse(
                'service := "local"\nmod ops "ops.pf"\n',
                filename=os.path.join(tmpdir, "Promptfile"),
            )

        task = pf.tasks["ops::deploy"]
        assert task.options.llm == "ops::openai"
        assert task.options.on == "ops::web"
        assert task.options.rollback == "ops::undo"
        assert "ops::checklist" in pf.functions
        assert "ops::openai" in pf.llm_providers
        assert "ops::web" in pf.host_groups
        resolved = pf.resolve_steps("ops::deploy")
        assert resolved[0].content == ("Follow module policy.\n\ninspect payments")


class TestWorkflowOptions:
    def test_postmortem_contracts_and_provider_strategy(self):
        pf = parse("""\
llm primary [template="primary {prompt}"]
llm backup [template="backup {prompt}"]

task diagnose:
    diagnose

task build [postmortem=diagnose, fallback-llm=backup, retries=2, requires="seed.stdout:json", produces=object]:
    build
""")
        options = pf.tasks["build"].options
        assert options.postmortem == "diagnose"
        assert options.fallback_llms == ["backup"]
        assert options.retries == 2
        assert options.requires == ["seed.stdout:json"]
        assert options.produces == "object"

    def test_unknown_fallback_provider_is_rejected(self):
        with pytest.raises(ParseError, match="unknown fallback LLM"):
            parse("""\
task review [fallback-llm=missing]:
    review
""")

    def test_invalid_retry_count_is_rejected(self):
        with pytest.raises(ParseError, match="between 0 and 10"):
            parse("""\
task review [retries=99]:
            review
""")

    def test_fallback_providers_are_deduplicated(self):
        pf = parse("""\
llm backup [template="backup {prompt}"]

task review [fallback-llm="backup|backup"]:
    review
""")

        assert pf.tasks["review"].options.fallback_llms == ["backup"]

    def test_too_many_fallback_providers_are_rejected(self):
        with pytest.raises(ParseError, match="at most 4"):
            parse("""\
llm one [template="one {prompt}"]
llm two [template="two {prompt}"]
llm three [template="three {prompt}"]
llm four [template="four {prompt}"]
llm five [template="five {prompt}"]

task review [fallback-llm="one|two|three|four|five"]:
    review
""")

    @pytest.mark.parametrize("value", ["0", "1000001"])
    def test_invalid_max_tokens_is_rejected(self, value):
        with pytest.raises(ParseError, match="max_tokens must be between"):
            parse(f"""\
task review [max_tokens={value}]:
    review
""")

    def test_invalid_artifact_contract_is_rejected(self):
        with pytest.raises(ParseError, match="artifact.field"):
            parse("""\
task publish [requires=missing]:
    publish
""")

        with pytest.raises(ParseError, match="unknown artifact contract type"):
            parse("""\
task publish [requires=build.stdout:yaml]:
    publish
""")


class TestRepairOption:
    def test_parses_repair_count(self):
        pf = parse("""\
task inspect [produces=object, repair=2]:
    inspect it
""")
        assert pf.tasks["inspect"].options.repair == 2

    def test_defaults_to_zero(self):
        pf = parse("""\
task inspect [produces=object]:
    inspect it
""")
        assert pf.tasks["inspect"].options.repair == 0

    def test_rejects_non_integer(self):
        with pytest.raises(ParseError, match="repair must be an integer"):
            parse("""\
task inspect [repair=lots]:
    inspect it
""")

    def test_rejects_out_of_range(self):
        with pytest.raises(ParseError, match="repair must be between"):
            parse("""\
task inspect [repair=9]:
    inspect it
""")
