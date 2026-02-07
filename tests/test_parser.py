"""Tests for the Promptfile parser."""

import os
import tempfile

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

            main_src = f'include "common.pf"\n\ntask deploy: setup:\n    deploy\n'
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

            assert pf.variables["env"] == "production"
            assert "setup" in pf.tasks
            assert "deploy" in pf.tasks

    def test_include_functions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lib = os.path.join(tmpdir, "lib.pf")
            with open(lib, "w") as f:
                f.write("fn greet:\n    say hello\n")

            main_src = f'include "lib.pf"\n\ntask hello:\n    @use greet\n'
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))

            assert "greet" in pf.functions
            resolved = pf.resolve_prompt("hello")
            assert resolved == "say hello"

    def test_include_not_found_raises(self):
        src = 'include "nonexistent.pf"\n'
        with pytest.raises(ParseError, match="not found"):
            parse(src)

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

            main_src = f'include "common.pf"\nenv := "override"\n\ntask show:\n    env is {{{{env}}}}\n'
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
        assert fast_provider.name == "openai"
        default_provider = pf.get_llm_for_task("default-task")
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
        assert deploy_llm.name == "openai"
        build_llm = pf.get_llm_for_task("build")
        assert build_llm.name == "claude"  # default

        # Host resolution
        assert pf.get_hosts_for_task("deploy").name == "web"
        assert pf.get_hosts_for_task("backup").name == "db"
        assert pf.get_hosts_for_task("build") is None
