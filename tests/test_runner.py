"""Tests for the task runner and dependency resolution."""

import os
import subprocess
import tempfile
from unittest.mock import patch

import pytest

from promptfile.parser import parse
from promptfile.runner import Runner, StepResult, topological_sort, CycleError
from promptfile.dispatcher import DryRunDispatcher
from promptfile.models import TaskStep


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

class TestTopologicalSort:
    def test_no_deps(self):
        pf = parse("""\
task build:
    build it
""")
        assert topological_sort(pf, "build") == ["build"]

    def test_linear_deps(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b

task c: b:
    do c
""")
        order = topological_sort(pf, "c")
        assert order == ["a", "b", "c"]

    def test_diamond_deps(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b

task c: a:
    do c

task d: b c:
    do d
""")
        order = topological_sort(pf, "d")
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")
        assert len(order) == 4

    def test_cycle_detection(self):
        pf = parse("""\
task a:
    do a

task b:
    do b
""")
        pf.tasks["a"].dependencies = ["b"]
        pf.tasks["b"].dependencies = ["a"]

        with pytest.raises(CycleError, match="cycle"):
            topological_sort(pf, "a")

    def test_self_cycle(self):
        pf = parse("""\
task a:
    do a
""")
        pf.tasks["a"].dependencies = ["a"]

        with pytest.raises(CycleError):
            topological_sort(pf, "a")

    def test_only_needed_tasks_included(self):
        pf = parse("""\
task a:
    do a

task b:
    do b

task c: a:
    do c
""")
        order = topological_sort(pf, "c")
        assert "b" not in order
        assert order == ["a", "c"]


# ---------------------------------------------------------------------------
# Runner — basic
# ---------------------------------------------------------------------------

class TestRunner:
    def test_run_single_task(self):
        pf = parse("""\
task build:
    build the project
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        assert len(result.task_results) == 1
        assert result.task_results[0].task_name == "build"
        assert result.task_results[0].prompt_sent == "build the project"

    def test_run_default_task(self):
        pf = parse("""\
task first:
    first task prompt

task second:
    second task prompt
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run()

        assert result.target == "first"
        assert len(result.task_results) == 1
        assert result.task_results[0].task_name == "first"

    def test_run_with_deps_executes_in_order(self):
        pf = parse("""\
task build:
    build

task test: build:
    test

task deploy: test:
    deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        assert result.success
        names = [r.task_name for r in result.task_results]
        assert names == ["build", "test", "deploy"]

    def test_run_with_variable_interpolation(self):
        pf = parse("""\
project := "acme"

task deploy:
    deploy {{project}} now
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        assert result.task_results[0].prompt_sent == "deploy acme now"

    def test_run_unknown_task_raises(self):
        pf = parse("""\
task build:
    build
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with pytest.raises(KeyError, match="unknown task"):
            runner.run("nonexistent")

    def test_run_empty_promptfile_raises(self):
        pf = parse("")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with pytest.raises(ValueError, match="no tasks defined"):
            runner.run()

    def test_dispatcher_receives_all_tasks(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        runner.run("b")

        assert len(dispatcher.dispatched) == 2
        assert dispatcher.dispatched[0][1].name == "a"
        assert dispatcher.dispatched[1][1].name == "b"

    def test_failure_stops_execution(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b
""")

        class FailDispatcher(DryRunDispatcher):
            def dispatch(self, prompt, task):
                result = super().dispatch(prompt, task)
                if task.name == "a":
                    result.success = False
                return result

        dispatcher = FailDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("b")

        assert not result.success
        assert len(result.task_results) == 1
        assert result.task_results[0].task_name == "a"


# ---------------------------------------------------------------------------
# Runner — shell steps
# ---------------------------------------------------------------------------

class TestRunnerShellSteps:
    def test_shell_step_executes(self):
        pf = parse("""\
task hello:
    !echo hello-from-shell
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("hello")

        assert result.success
        assert len(result.task_results) == 1
        tr = result.task_results[0]
        assert len(tr.step_results) == 1
        assert tr.step_results[0].kind == "shell"
        assert "hello-from-shell" in tr.step_results[0].response

    def test_interleaved_shell_and_prompt(self):
        pf = parse("""\
task mixed:
    !echo step-one
    analyze the output
    !echo step-three
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("mixed")

        assert result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 3
        assert sr[0].kind == "shell"
        assert "step-one" in sr[0].response
        assert sr[1].kind == "prompt"
        assert sr[2].kind == "shell"
        assert "step-three" in sr[2].response

    def test_shell_failure_stops_task(self):
        pf = parse("""\
task bad:
    !false
    this prompt should not run
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("bad")

        assert not result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 1  # only the shell step ran
        assert sr[0].kind == "shell"

    def test_ignore_continues_on_failure(self):
        pf = parse("""\
task resilient:
    !@ignore false
    this prompt should still run
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("resilient")

        assert result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 2
        assert sr[0].kind == "shell"
        assert sr[1].kind == "prompt"

    def test_silent_suppresses_output(self):
        pf = parse("""\
task quiet:
    !@silent echo this-should-be-hidden
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("quiet")

        assert result.success
        sr = result.task_results[0].step_results
        assert sr[0].response == ""  # output suppressed

    def test_shell_with_variable_interpolation(self):
        pf = parse("""\
project := "myapp"

task build:
    !echo building {{project}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        assert "building myapp" in result.task_results[0].step_results[0].response


# ---------------------------------------------------------------------------
# Runner — task arguments
# ---------------------------------------------------------------------------

class TestRunnerArgs:
    def test_args_passed_to_prompt(self):
        pf = parse("""\
task greet(name):
    say hello to {{name}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("greet", args={"name": "Bob"})

        assert result.success
        assert "Bob" in result.task_results[0].prompt_sent

    def test_args_with_defaults(self):
        pf = parse("""\
task deploy(target="localhost"):
    deploy to {{target}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        assert result.success
        assert "localhost" in result.task_results[0].prompt_sent

    def test_args_override_defaults(self):
        pf = parse("""\
task deploy(target="localhost"):
    deploy to {{target}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy", args={"target": "prod"})

        assert result.success
        assert "prod" in result.task_results[0].prompt_sent

    def test_args_in_shell_steps(self):
        pf = parse("""\
task build(env):
    !echo building for {{env}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build", args={"env": "production"})

        assert result.success
        assert "building for production" in result.task_results[0].step_results[0].response

    def test_args_not_passed_to_deps(self):
        pf = parse("""\
task setup:
    setup base

task deploy(target): setup:
    deploy to {{target}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy", args={"target": "prod"})

        assert result.success
        # setup should not get the 'target' arg — it should resolve cleanly
        assert result.task_results[0].task_name == "setup"
        assert result.task_results[1].task_name == "deploy"


# ---------------------------------------------------------------------------
# Runner — functions (@use)
# ---------------------------------------------------------------------------

class TestRunnerFunctions:
    def test_use_expands_in_prompt(self):
        pf = parse("""\
fn preamble:
    You are a senior engineer.

task review:
    @use preamble
    Review the code.
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("review")

        assert result.success
        prompt = result.task_results[0].prompt_sent
        assert "senior engineer" in prompt
        assert "Review the code" in prompt


# ---------------------------------------------------------------------------
# Runner — docker tasks
# ---------------------------------------------------------------------------

class TestRunnerDocker:
    def test_docker_sends_generate_prompt(self):
        pf = parse("""\
docker myapp:
    Python 3.11 slim image.
    Install requirements.txt.
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with tempfile.TemporaryDirectory() as tmpdir:
            pf.tasks["myapp"].docker.context = tmpdir
            result = runner.run("myapp")

        # The dispatcher should have received a "Generate a Dockerfile" prompt
        assert len(dispatcher.dispatched) == 1
        prompt_sent = dispatcher.dispatched[0][0]
        assert "Generate a Dockerfile" in prompt_sent
        assert "Python 3.11" in prompt_sent

    def test_docker_step_results(self):
        pf = parse("""\
docker myapp:
    Python 3.11 slim image.
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with tempfile.TemporaryDirectory() as tmpdir:
            pf.tasks["myapp"].docker.context = tmpdir
            result = runner.run("myapp")

        tr = result.task_results[0]
        # Should have docker-generate and docker-build steps
        kinds = [sr.kind for sr in tr.step_results]
        assert "docker-generate" in kinds
        assert "docker-build" in kinds

    def test_docker_as_dependency(self):
        pf = parse("""\
docker myapp:
    Python 3.11 slim image.

task deploy: myapp:
    push the image
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        mock_result = subprocess.CompletedProcess(args="docker build", returncode=0, stdout="built\n", stderr="")
        with tempfile.TemporaryDirectory() as tmpdir:
            pf.tasks["myapp"].docker.context = tmpdir
            with patch("promptfile.runner.subprocess.run", return_value=mock_result):
                result = runner.run("deploy")

        names = [tr.task_name for tr in result.task_results]
        assert names[0] == "myapp"
        assert names[1] == "deploy"


# ---------------------------------------------------------------------------
# Runner — SSH / hosts execution
# ---------------------------------------------------------------------------

class TestRunnerSSH:
    def test_ssh_step_runs_on_each_host(self):
        pf = parse("""\
hosts web:
    host1
    host2

task deploy [on=web]:
    !uptime
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        mock_result = subprocess.CompletedProcess(
            args="ssh", returncode=0, stdout="up 5 days\n", stderr=""
        )
        with patch("promptfile.runner.subprocess.run", return_value=mock_result):
            result = runner.run("deploy")

        assert result.success
        sr = result.task_results[0].step_results
        # Should have one SSH step per host
        assert len(sr) == 2
        assert sr[0].kind == "ssh"
        assert sr[0].host == "host1"
        assert sr[1].kind == "ssh"
        assert sr[1].host == "host2"

    def test_ssh_builds_correct_command(self):
        from promptfile.runner import _build_ssh_command
        from promptfile.models import HostGroup

        group = HostGroup(name="web", hosts=["h1"], user="deploy", port=2222)
        cmd = _build_ssh_command("h1", "uptime", group)
        assert "ssh" in cmd
        assert "-p 2222" in cmd
        assert "deploy@h1" in cmd
        assert "uptime" in cmd

    def test_ssh_without_user(self):
        from promptfile.runner import _build_ssh_command
        from promptfile.models import HostGroup

        group = HostGroup(name="web", hosts=["h1"])
        cmd = _build_ssh_command("h1", "ls", group)
        assert "h1" in cmd
        assert "@" not in cmd  # no user@ prefix

    def test_prompt_steps_run_locally_even_with_hosts(self):
        pf = parse("""\
hosts web:
    host1

task check [on=web]:
    !uptime
    analyze the uptime output
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        mock_result = subprocess.CompletedProcess(
            args="ssh", returncode=0, stdout="up\n", stderr=""
        )
        with patch("promptfile.runner.subprocess.run", return_value=mock_result):
            result = runner.run("check")

        assert result.success
        sr = result.task_results[0].step_results
        assert sr[0].kind == "ssh"      # shell runs via SSH
        assert sr[1].kind == "prompt"   # prompt runs locally

    def test_ssh_failure_stops_execution(self):
        pf = parse("""\
hosts web:
    host1
    host2

task deploy [on=web]:
    !systemctl restart myapp
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        mock_result = subprocess.CompletedProcess(
            args="ssh", returncode=1, stdout="", stderr="failed\n"
        )
        with patch("promptfile.runner.subprocess.run", return_value=mock_result):
            result = runner.run("deploy")

        assert not result.success
        sr = result.task_results[0].step_results
        # Should stop after first host failure
        assert len(sr) == 1
        assert sr[0].host == "host1"


# ---------------------------------------------------------------------------
# Runner — LLM provider routing
# ---------------------------------------------------------------------------

class TestRunnerLLMRouting:
    def test_per_task_llm_routes_to_different_dispatcher(self):
        pf = parse("""\
llm claude [model=opus]
llm openai [model=gpt-4]

task review [llm=openai]:
    review the code
""")
        # The runner dispatcher is the fallback; per-task should override
        fallback = DryRunDispatcher()
        runner = Runner(pf, fallback)

        # _get_dispatcher should return a different dispatcher for the task
        task = pf.tasks["review"]
        task_dispatcher = runner._get_dispatcher(task)
        # It should NOT be the fallback dry-run dispatcher
        assert task_dispatcher is not fallback

    def test_default_llm_used_when_no_per_task_override(self):
        pf = parse("""\
llm claude [model=opus]

task build:
    build it
""")
        fallback = DryRunDispatcher()
        runner = Runner(pf, fallback)

        task = pf.tasks["build"]
        task_dispatcher = runner._get_dispatcher(task)
        # Has a configured LLM, so it should use that, not the fallback
        assert task_dispatcher is not fallback

    def test_no_llm_configured_uses_fallback(self):
        pf = parse("""\
task build:
    build it
""")
        fallback = DryRunDispatcher()
        runner = Runner(pf, fallback)

        task = pf.tasks["build"]
        task_dispatcher = runner._get_dispatcher(task)
        # No LLM configured, should fall back to the provided dispatcher
        assert task_dispatcher is fallback


# ---------------------------------------------------------------------------
# Runner — alias resolution
# ---------------------------------------------------------------------------

class TestRunnerAliases:
    def test_alias_resolves_to_task(self):
        pf = parse("""\
task deploy:
    deploy it

alias d := deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("d")

        assert result.success
        assert result.target == "deploy"
        assert result.task_results[0].task_name == "deploy"

    def test_alias_with_args(self):
        pf = parse("""\
task deploy(target):
    deploy to {{target}}

alias d := deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("d", args={"target": "prod"})

        assert result.success
        assert "prod" in result.task_results[0].prompt_sent


# ---------------------------------------------------------------------------
# Runner — OS filter
# ---------------------------------------------------------------------------

class TestRunnerOsFilter:
    def test_os_filter_skips_non_matching(self):
        pf = parse("""\
task linuxonly [os=freebsd]:
    do freebsd stuff
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("linuxonly")

        assert result.success
        assert "[skipped]" in result.task_results[0].response

    def test_os_filter_dep_skipped(self):
        pf = parse("""\
task platform_specific [os=freebsd]:
    platform specific stuff

task main: platform_specific:
    main logic
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("main")

        assert result.success
        assert len(result.task_results) == 2
        assert "[skipped]" in result.task_results[0].response
        assert result.task_results[1].task_name == "main"


# ---------------------------------------------------------------------------
# Runner — confirm
# ---------------------------------------------------------------------------

class TestRunnerConfirm:
    def test_confirm_declined_skips(self):
        pf = parse("""\
task deploy [confirm]:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        # Mock input to return "n"
        with patch("builtins.input", return_value="n"):
            result = runner.run("deploy")

        assert result.success
        assert "[skipped]" in result.task_results[0].response

    def test_confirm_accepted_runs(self):
        pf = parse("""\
task deploy [confirm]:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with patch("builtins.input", return_value="y"):
            result = runner.run("deploy")

        assert result.success
        assert "[skipped]" not in result.task_results[0].response

    def test_confirm_with_custom_message(self):
        pf = parse("""\
task deploy [confirm=Really deploy?]:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        captured_prompts = []
        def mock_input(prompt):
            captured_prompts.append(prompt)
            return "y"

        with patch("builtins.input", side_effect=mock_input):
            result = runner.run("deploy")

        assert result.success
        assert "Really deploy?" in captured_prompts[0]


# ---------------------------------------------------------------------------
# Runner — working directory
# ---------------------------------------------------------------------------

class TestRunnerWorkingDir:
    def test_task_working_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pf = parse(f"""\
task build [working-dir={tmpdir}]:
    !pwd
""")
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("build")

            assert result.success
            assert tmpdir in result.task_results[0].step_results[0].response

    def test_global_working_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pf = parse(f"""\
set working-dir "{tmpdir}"

task build:
    !pwd
""")
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("build")

            assert result.success
            assert tmpdir in result.task_results[0].step_results[0].response


# ---------------------------------------------------------------------------
# Runner — dotenv loading
# ---------------------------------------------------------------------------

class TestRunnerDotenv:
    def test_dotenv_loads_env_vars(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create .env file
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w") as f:
                f.write('PF_DOTENV_TEST=loaded_value\n')

            pf = parse("""\
set dotenv-load

task show:
    value is ${PF_DOTENV_TEST}
""")
            # Remove the env var if it exists
            os.environ.pop("PF_DOTENV_TEST", None)

            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)

            # Change to tmpdir to find .env
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                result = runner.run("show")
            finally:
                os.chdir(old_cwd)
                os.environ.pop("PF_DOTENV_TEST", None)

            assert result.success
            assert "loaded_value" in result.task_results[0].prompt_sent


# ---------------------------------------------------------------------------
# Runner — set shell
# ---------------------------------------------------------------------------

class TestRunnerShell:
    def test_set_shell_used_for_commands(self):
        pf = parse("""\
set shell "/bin/sh"

task build:
    !echo hello
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        assert "hello" in result.task_results[0].step_results[0].response
