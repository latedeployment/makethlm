"""Tests for the Promptfile parser."""

import os
import tempfile

import pytest

from justprompt.parser import parse, ParseError


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
        from justprompt.models import TaskOptions
        opts = TaskOptions()
        assert opts.should_skip_for_os() is False

    def test_matching_os_does_not_skip(self):
        import platform
        from justprompt.models import TaskOptions
        current = platform.system().lower()
        # Map back to our naming
        reverse_map = {"linux": "linux", "darwin": "macos", "windows": "windows"}
        os_name = reverse_map.get(current, current)
        opts = TaskOptions(os_filter=os_name)
        assert opts.should_skip_for_os() is False

    def test_non_matching_os_skips(self):
        from justprompt.models import TaskOptions
        # Use a platform that definitely won't match
        opts = TaskOptions(os_filter="freebsd")
        assert opts.should_skip_for_os() is True


# ---------------------------------------------------------------------------
# TaskOptions merge
# ---------------------------------------------------------------------------

class TestTaskOptionsMerge:
    def test_merge_basic(self):
        from justprompt.models import TaskOptions
        base = TaskOptions(model="base-model", group="ci")
        override = TaskOptions(model="new-model")
        merged = base.merge(override)
        assert merged.model == "new-model"
        assert merged.group == "ci"

    def test_merge_private(self):
        from justprompt.models import TaskOptions
        base = TaskOptions(private=False)
        override = TaskOptions(private=True)
        merged = base.merge(override)
        assert merged.private is True

    def test_merge_confirm(self):
        from justprompt.models import TaskOptions
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

    def test_set_fallback(self):
        pf = parse("""\
set fallback

task build:
    build it
""")
        assert pf.settings.fallback is True

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

    def test_set_dotenv_required(self):
        pf = parse("""\
set dotenv-required

task build:
    build it
""")
        assert pf.settings.dotenv_required is True


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
        from justprompt.models import TaskOptions
        import platform
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
