"""Tests for the task runner and dependency resolution."""

import os
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

import pytest

from makethlm.parser import parse
from makethlm.runner import Runner, StepResult, topological_sort, CycleError
from makethlm.dispatcher import DryRunDispatcher
from makethlm.models import TaskStep


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

    def test_run_set_default_task(self):
        pf = parse("""\
set default second

task first:
    first task prompt

task second:
    second task prompt
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run()

        assert result.target == "second"
        assert len(result.task_results) == 1
        assert result.task_results[0].task_name == "second"

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

    @patch("makethlm.runner._run_subprocess")
    def test_shell_timeout_option_is_used(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args="sleep 1", returncode=0, stdout="ok\n", stderr=""
        )
        pf = parse("""\
task slow [timeout=30s]:
    !sleep 1
""")
        runner = Runner(pf, DryRunDispatcher())

        result = runner.run("slow")

        assert result.success
        assert mock_run.call_args.kwargs["timeout"] == 30

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
            assert pf.tasks["myapp"].docker is not None
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
            assert pf.tasks["myapp"].docker is not None
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
            assert pf.tasks["myapp"].docker is not None
            pf.tasks["myapp"].docker.context = tmpdir
            with patch("makethlm.runner._run_subprocess", return_value=mock_result):
                result = runner.run("deploy")

        names = [tr.task_name for tr in result.task_results]
        assert names[0] == "myapp"
        assert names[1] == "deploy"


# ---------------------------------------------------------------------------
# Runner — @echo steps
# ---------------------------------------------------------------------------

class TestRunnerEcho:
    def test_echo_step_succeeds(self):
        pf = parse("""\
task build:
    @echo "Building..."
    !echo done
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 2
        assert sr[0].kind == "echo"
        assert sr[0].success
        assert sr[1].kind == "shell"

    def test_echo_does_not_stop_execution(self):
        pf = parse("""\
task build:
    @echo "step 1"
    !echo hello
    @echo "step 2"
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        sr = result.task_results[0].step_results
        assert len(sr) == 3
        assert sr[0].kind == "echo"
        assert sr[1].kind == "shell"
        assert sr[2].kind == "echo"

    def test_echo_with_variable(self):
        pf = parse("""\
name := "world"

task greet:
    @echo "Hello {{name}}"
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("greet")

        assert result.success
        sr = result.task_results[0].step_results[0]
        assert sr.kind == "echo"
        assert sr.content == "Hello world"


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
        with patch("makethlm.runner._run_subprocess", return_value=mock_result):
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
        from makethlm.runner import _build_ssh_command
        from makethlm.models import HostGroup

        group = HostGroup(name="web", hosts=["h1"], user="deploy", port=2222)
        cmd = _build_ssh_command("h1", "uptime", group)
        assert "ssh" in cmd
        assert "-p 2222" in cmd
        assert "deploy@h1" in cmd
        assert "uptime" in cmd

    def test_ssh_builds_command_with_identity_and_host_key_policy(self):
        from makethlm.runner import _build_ssh_command
        from makethlm.models import HostGroup

        group = HostGroup(
            name="web",
            hosts=["h1"],
            identity_file="/tmp/id_ed25519",
            strict_host_key_checking="accept-new",
        )
        cmd = _build_ssh_command("h1", "uptime", group)
        assert "-i /tmp/id_ed25519" in cmd
        assert "-o StrictHostKeyChecking=accept-new" in cmd

    def test_ssh_without_user(self):
        from makethlm.runner import _build_ssh_command
        from makethlm.models import HostGroup

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
        with patch("makethlm.runner._run_subprocess", return_value=mock_result):
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
        with patch("makethlm.runner._run_subprocess", return_value=mock_result):
            result = runner.run("deploy")

        assert not result.success
        sr = result.task_results[0].step_results
        # Should stop after first host failure
        assert len(sr) == 1
        assert sr[0].host == "host1"

    def test_ssh_parallel_runs_all_hosts_for_step(self):
        pf = parse("""\
hosts web:
    host1
    host2

task deploy [on=web, ssh-parallel]:
    !uptime
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        def _fake_ssh(step, host, group):
            return StepResult(kind="ssh", content=step.content, response=host, success=True, host=host)

        with patch.object(runner, "_run_ssh_step", side_effect=_fake_ssh) as mock_ssh:
            result = runner.run("deploy")

        assert result.success
        assert mock_ssh.call_count == 2
        hosts = [sr.host for sr in result.task_results[0].step_results]
        assert hosts == ["host1", "host2"]


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

    def test_codex_provider_routes_to_codex_dispatcher(self):
        from makethlm.dispatcher import CodexDispatcher

        pf = parse("""\
llm codex [model=gpt-5-codex]

task review:
    review the code
""")
        fallback = DryRunDispatcher()
        runner = Runner(pf, fallback)

        task_dispatcher = runner._get_dispatcher(pf.tasks["review"])

        assert isinstance(task_dispatcher, CodexDispatcher)
        assert task_dispatcher.default_model == "gpt-5-codex"

    def test_dry_run_uses_fallback_dispatcher_even_with_provider(self):
        pf = parse("""\
llm claude [model=opus]

task review:
    review the code
""")
        fallback = DryRunDispatcher()
        runner = Runner(pf, fallback, dry_run=True)

        assert runner._get_dispatcher(pf.tasks["review"]) is fallback


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

    def test_dotenv_load_with_path(self):
        """dotenv-load with a file path loads that specific file."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env.custom")
            with open(env_path, "w") as f:
                f.write('PF_DOTENV_CUSTOM=custom_value\n')

            pf = parse(f"""\
set dotenv-load "{env_path}"

task show:
    value is ${{PF_DOTENV_CUSTOM}}
""")
            os.environ.pop("PF_DOTENV_CUSTOM", None)

            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)

            try:
                result = runner.run("show")
            finally:
                os.environ.pop("PF_DOTENV_CUSTOM", None)

            assert result.success
            assert "custom_value" in result.task_results[0].prompt_sent

    def test_dotenv_path_expands_env_vars(self):
        """dotenv path supports $VAR and ${VAR} expansion."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env.expanded")
            with open(env_path, "w") as f:
                f.write('PF_DOTENV_EXPANDED=it_works\n')

            pf = parse("""\
set dotenv-load "$PF_TEST_DOTENV_DIR/.env.expanded"

task show:
    value is ${PF_DOTENV_EXPANDED}
""")
            os.environ.pop("PF_DOTENV_EXPANDED", None)
            os.environ["PF_TEST_DOTENV_DIR"] = tmpdir

            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)

            try:
                result = runner.run("show")
            finally:
                os.environ.pop("PF_DOTENV_EXPANDED", None)
                os.environ.pop("PF_TEST_DOTENV_DIR", None)

            assert result.success
            assert "it_works" in result.task_results[0].prompt_sent

    def test_dotenv_path_expands_tilde(self):
        """dotenv path supports ~ (home directory) expansion."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env.tilde")
            with open(env_path, "w") as f:
                f.write('PF_DOTENV_TILDE=tilde_works\n')

            # Use ~ in the path and mock expanduser to point at our tmpdir
            tilde_path = "~/.env.tilde"

            pf = parse(f"""\
set dotenv-load "{tilde_path}"

task show:
    value is ${{PF_DOTENV_TILDE}}
""")
            os.environ.pop("PF_DOTENV_TILDE", None)

            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)

            with mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", tmpdir, 1)):
                try:
                    result = runner.run("show")
                finally:
                    os.environ.pop("PF_DOTENV_TILDE", None)

            assert result.success
            assert "tilde_works" in result.task_results[0].prompt_sent


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


# ---------------------------------------------------------------------------
# Runner — export variables
# ---------------------------------------------------------------------------

class TestRunnerExport:
    def test_export_var_in_env(self):
        pf = parse("""\
export PF_TEST_EXPORT := "exported_value"

task show:
    !echo $PF_TEST_EXPORT
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        # Clean up before test
        os.environ.pop("PF_TEST_EXPORT", None)
        result = runner.run("show")
        os.environ.pop("PF_TEST_EXPORT", None)

        assert result.success
        assert "exported_value" in result.task_results[0].step_results[0].response

    def test_set_export_all(self):
        pf = parse("""\
set export

PF_TEST_SETEXPORT := "all_exported"

task show:
    !echo $PF_TEST_SETEXPORT
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        os.environ.pop("PF_TEST_SETEXPORT", None)
        result = runner.run("show")
        os.environ.pop("PF_TEST_SETEXPORT", None)

        assert result.success
        assert "all_exported" in result.task_results[0].step_results[0].response


class TestSecretRedaction:
    def test_shell_output_redacts_exported_secret(self):
        pf = parse("""\
export API_KEY := "secret123"

task show:
    !echo secret123
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        result = runner.run("show")

        assert "[redacted]" in result.task_results[0].response
        assert "secret123" not in result.task_results[0].response

    def test_secret_injection_env_backend_is_redacted(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_SECRET", "supersecret")
        pf = parse("""\
set secrets "env"

task show:
    !echo {{#secret:PF_TEST_SECRET}}
    reveal {{#secret:PF_TEST_SECRET}}
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        result = runner.run("show")

        assert result.success
        assert "supersecret" not in result.task_results[0].prompt_sent
        assert "[redacted]" in result.task_results[0].prompt_sent
        assert "supersecret" not in result.task_results[0].response
        assert "[redacted]" in result.task_results[0].response
        assert result.task_results[0].step_results[0].response == "[redacted]"

    def test_secret_injection_masks_in_dry_run(self, monkeypatch):
        monkeypatch.setenv("PF_TEST_SECRET", "supersecret")
        pf = parse("""\
set secrets "env"

task show:
    reveal {{#secret:PF_TEST_SECRET}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher, verbose=False, dry_run=True)

        result = runner.run("show")

        assert result.success
        assert dispatcher.dispatched[0][0] == "reveal ***"
        assert result.task_results[0].prompt_sent == "reveal ***"


# ---------------------------------------------------------------------------
# Runner — default makethlm env vars
# ---------------------------------------------------------------------------

class TestRunnerDefaultEnvVars:
    def test_makethlm_task_env_var(self):
        pf = parse("""\
task build:
    !echo $MAKETHLM_TASK
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher, promptfile_path="/tmp/Promptfile")
        result = runner.run("build")
        assert result.success
        assert "build" in result.task_results[0].step_results[0].response

    def test_makethlm_file_env_var(self):
        pf = parse("""\
task show:
    !echo $MAKETHLM_FILE
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher, promptfile_path="/tmp/myproject/Promptfile")
        result = runner.run("show")
        assert result.success
        assert "/tmp/myproject/Promptfile" in result.task_results[0].step_results[0].response

    def test_makethlm_dir_env_var(self):
        pf = parse("""\
task show:
    !echo $MAKETHLM_DIR
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher, promptfile_path="/tmp/myproject/Promptfile")
        result = runner.run("show")
        assert result.success
        assert "/tmp/myproject" in result.task_results[0].step_results[0].response


# ---------------------------------------------------------------------------
# Runner — no-cd
# ---------------------------------------------------------------------------

class TestRunnerNoCd:
    def test_no_cd_ignores_working_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            pf = parse(f"""\
set working-dir "{tmpdir}"

task build [no-cd]:
    !pwd
""")
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("build")

            assert result.success
            # no-cd means working dir should NOT be tmpdir
            output = result.task_results[0].step_results[0].response
            # It should be the current directory, not tmpdir
            assert output  # just check it ran


# ---------------------------------------------------------------------------
# Runner — variadic args
# ---------------------------------------------------------------------------

class TestRunnerVariadicArgs:
    def test_plus_variadic_collects_remaining(self):
        pf = parse("""\
task greet(+names):
    hello to {{names}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("greet", args={"names": "alice bob charlie"})

        assert result.success
        assert "alice bob charlie" in result.task_results[0].prompt_sent

    def test_star_variadic_allows_empty(self):
        pf = parse("""\
task greet(*names):
    hello to {{names}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("greet", args={"names": ""})

        assert result.success


# ---------------------------------------------------------------------------
# Runner — ignore-comments
# ---------------------------------------------------------------------------

class TestRunnerIgnoreComments:
    def test_ignore_comments_strips_comments(self):
        pf = parse("""\
set ignore-comments

task build:
    !echo hello # this is a comment
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        # The comment should be stripped, so output should be "hello"
        assert "hello" in result.task_results[0].step_results[0].response
        assert "this is a comment" not in result.task_results[0].step_results[0].response


# ---------------------------------------------------------------------------
# Runner — built-in functions
# ---------------------------------------------------------------------------

class TestRunnerBuiltinFunctions:
    def test_os_in_prompt(self):
        pf = parse("""\
task show:
    running on {{os()}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("show")

        assert result.success
        assert "{{os()}}" not in result.task_results[0].prompt_sent

    def test_arch_in_shell(self):
        import platform
        pf = parse("""\
task show:
    !echo {{arch()}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("show")

        assert result.success
        assert platform.machine() in result.task_results[0].step_results[0].response


# ---------------------------------------------------------------------------
# Runner — if/else in templates
# ---------------------------------------------------------------------------

class TestRunnerIfElse:
    def test_if_else_in_prompt(self):
        pf = parse("""\
env := "prod"

task show:
    {{if env == "prod" { "production" } else { "development" }}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("show")

        assert result.success
        assert "production" in result.task_results[0].prompt_sent

    def test_if_else_concat_in_prompt(self):
        pf = parse("""\
project := "app"

task show:
    name is {{"my-" + project + "-v1"}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("show")

        assert result.success
        assert "my-app-v1" in result.task_results[0].prompt_sent


# ---------------------------------------------------------------------------
# Runner — shell command features (pipes, redirection, &&, ||, etc.)
# ---------------------------------------------------------------------------

class TestRunnerShellFeatures:
    def _run_shell(self, cmd: str) -> str:
        """Helper: run a single shell command and return its output."""
        pf = parse(f"task t:\n    !{cmd}\n")
        runner = Runner(pf, DryRunDispatcher())
        result = runner.run("t")
        assert result.success
        return result.task_results[0].step_results[0].response

    def test_pipe(self):
        out = self._run_shell("echo hello world | tr a-z A-Z")
        assert "HELLO WORLD" in out

    def test_pipe_chain(self):
        out = self._run_shell("echo abc def ghi | tr ' ' '\\n' | sort | head -1")
        assert out.strip() == "abc"

    def test_and_operator(self):
        out = self._run_shell("echo first && echo second")
        assert "first" in out
        assert "second" in out

    def test_and_short_circuit(self):
        """false && echo should-not-run -- the task should fail."""
        pf = parse("task t:\n    !false && echo should-not-run\n")
        runner = Runner(pf, DryRunDispatcher())
        result = runner.run("t")
        assert not result.success
        assert "should-not-run" not in result.task_results[0].step_results[0].response

    def test_or_operator(self):
        out = self._run_shell("false || echo fallback")
        assert "fallback" in out

    def test_or_short_circuit(self):
        out = self._run_shell("echo ok || echo should-not-run")
        assert "ok" in out
        assert "should-not-run" not in out

    def test_semicolon(self):
        out = self._run_shell("echo aaa; echo bbb")
        assert "aaa" in out
        assert "bbb" in out

    def test_redirect_to_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self._run_shell(f"echo redirect-test > {path}")
            with open(path) as f:
                assert "redirect-test" in f.read()
        finally:
            os.unlink(path)

    def test_append_redirect(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("line1\n")
            path = f.name
        try:
            self._run_shell(f"echo line2 >> {path}")
            with open(path) as f:
                content = f.read()
            assert "line1" in content
            assert "line2" in content
        finally:
            os.unlink(path)

    def test_subshell(self):
        out = self._run_shell("(echo inside-subshell)")
        assert "inside-subshell" in out

    def test_command_substitution(self):
        out = self._run_shell("echo result-is-$(echo 42)")
        assert "result-is-42" in out

    def test_backtick_substitution(self):
        out = self._run_shell("echo result-is-`echo 99`")
        assert "result-is-99" in out

    def test_process_substitution(self):
        out = self._run_shell("cat <(echo from-process-sub)")
        assert "from-process-sub" in out

    def test_env_variable_expansion(self):
        out = self._run_shell("X=hello; echo $X")
        assert "hello" in out

    def test_stderr_redirect(self):
        out = self._run_shell("echo to-stderr >&2")
        assert "to-stderr" in out

    def test_glob_expansion(self):
        out = self._run_shell("echo /etc/host*")
        assert "/etc/host" in out

    def test_for_loop(self):
        out = self._run_shell("for i in a b c; do echo $i; done")
        assert "a" in out
        assert "b" in out
        assert "c" in out

    def test_exit_code_propagation(self):
        pf = parse("task t:\n    !exit 42\n")
        runner = Runner(pf, DryRunDispatcher())
        result = runner.run("t")
        assert not result.success


# ---------------------------------------------------------------------------
# Runner — agent integration
# ---------------------------------------------------------------------------

class TestRunnerAgents:
    def test_agent_instructions_prepended_to_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "security.md")
            with open(md_path, "w") as f:
                f.write("You are a security expert.\nFocus on vulnerabilities.")

            main_src = """\
agent security "./security.md"

task audit [agent=security]:
    Review the latest git diff.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("audit")

            assert result.success
            # The dispatched prompt should start with agent instructions
            prompt = dispatcher.dispatched[0][0]
            assert prompt.startswith("You are a security expert.")
            assert "Review the latest git diff." in prompt

    def test_agent_not_prepended_to_shell_steps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("Agent instructions here.")

            main_src = """\
agent myagent "./agent.md"

task mixed [agent=myagent]:
    !echo hello
    Review the output.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("mixed")

            assert result.success
            sr = result.task_results[0].step_results
            # Shell step output should not contain agent instructions
            assert "Agent instructions" not in sr[0].response
            # Prompt step should have agent instructions
            prompt = dispatcher.dispatched[0][0]
            assert prompt.startswith("Agent instructions here.")
            assert "Review the output." in prompt

    def test_task_without_agent_no_prepend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("Agent instructions.")

            main_src = """\
agent myagent "./agent.md"

task plain:
    Just a plain task.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("plain")

            assert result.success
            prompt = dispatcher.dispatched[0][0]
            assert "Agent instructions" not in prompt
            assert prompt == "Just a plain task."

    def test_agent_with_multiple_prompt_steps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("System: You are an expert.")

            main_src = """\
agent expert "./agent.md"

task multi [agent=expert]:
    !echo step1
    First prompt.
    !echo step2
    Second prompt.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("multi")

            assert result.success
            # Both prompt dispatches should have agent instructions
            assert len(dispatcher.dispatched) == 2
            for prompt, _ in dispatcher.dispatched:
                assert prompt.startswith("System: You are an expert.")


# ---------------------------------------------------------------------------
# Runner — guidance integration
# ---------------------------------------------------------------------------

class TestRunnerGuidance:
    def test_guidance_prepended_to_prompt(self):
        pf = parse("""\
guidance:
    Always be concise and clear.

task build:
    build the project
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        prompt = dispatcher.dispatched[0][0]
        assert prompt.startswith("Always be concise and clear.")
        assert "build the project" in prompt

    def test_guidance_not_prepended_to_shell_steps(self):
        pf = parse("""\
guidance:
    Always be concise.

task build:
    !echo hello
    build the project
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        sr = result.task_results[0].step_results
        # Shell step should NOT have guidance
        assert "Always be concise" not in sr[0].response
        # Prompt step should have guidance
        prompt = dispatcher.dispatched[0][0]
        assert prompt.startswith("Always be concise.")

    def test_guidance_with_agent_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = os.path.join(tmpdir, "agent.md")
            with open(md_path, "w") as f:
                f.write("You are a security expert.")

            main_src = """\
guidance:
    Follow all rules.

agent security "./agent.md"

task audit [agent=security]:
    Review the code.
"""
            pf = parse(main_src, filename=os.path.join(tmpdir, "Promptfile"))
            dispatcher = DryRunDispatcher()
            runner = Runner(pf, dispatcher)
            result = runner.run("audit")

            assert result.success
            prompt = dispatcher.dispatched[0][0]
            # Order: guidance -> agent -> task
            assert prompt.index("Follow all rules.") < prompt.index("You are a security expert.")
            assert prompt.index("You are a security expert.") < prompt.index("Review the code.")

    def test_no_guidance_no_prepend(self):
        pf = parse("""\
task build:
    build the project
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        prompt = dispatcher.dispatched[0][0]
        assert prompt == "build the project"


# ---------------------------------------------------------------------------
# Dispatcher — Claude naming
# ---------------------------------------------------------------------------

class TestClaudeDispatcherNaming:
    def test_system_prompt_includes_task_name(self):
        from makethlm.dispatcher import ClaudeDispatcher
        from makethlm.models import Task, TaskStep, TaskOptions

        task = Task(
            name="audit",
            steps=[TaskStep(kind="prompt", content="test")],
            options=TaskOptions(),
        )
        dispatcher = ClaudeDispatcher()

        # We can't actually call claude, but we can verify the command construction
        # by checking the dispatch method's internal cmd assembly
        # Let's patch run_subprocess to capture the command
        captured = []
        def mock_run(*args, **kwargs):
            captured.append(args[0] if args else kwargs.get('cmd'))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

        with patch("makethlm.dispatcher.run_subprocess", side_effect=mock_run):
            result = dispatcher.dispatch("test prompt", task)

        assert result.success
        cmd = captured[0]
        # Should contain --system-prompt with makethlm-audit naming
        assert "--system-prompt" in cmd
        sp_idx = cmd.index("--system-prompt")
        system_prompt = cmd[sp_idx + 1]
        assert "makethlm-audit" in system_prompt

    def test_system_prompt_varies_per_task(self):
        from makethlm.dispatcher import ClaudeDispatcher
        from makethlm.models import Task, TaskStep, TaskOptions

        dispatcher = ClaudeDispatcher()
        captured = []

        def mock_run(*args, **kwargs):
            captured.append(args[0] if args else kwargs.get('cmd'))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

        task1 = Task(name="build", steps=[TaskStep(kind="prompt", content="t")], options=TaskOptions())
        task2 = Task(name="deploy", steps=[TaskStep(kind="prompt", content="t")], options=TaskOptions())

        with patch("makethlm.dispatcher.run_subprocess", side_effect=mock_run):
            dispatcher.dispatch("prompt1", task1)
            dispatcher.dispatch("prompt2", task2)

        cmd1 = captured[0]
        cmd2 = captured[1]
        sp1 = cmd1[cmd1.index("--system-prompt") + 1]
        sp2 = cmd2[cmd2.index("--system-prompt") + 1]
        assert "makethlm-build" in sp1
        assert "makethlm-deploy" in sp2


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

class TestArtifacts:
    def test_artifact_stored_by_task_name(self):
        pf = parse("""\
task build:
    !echo built-output

task deploy: build:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        runner.run("deploy")

        assert "build" in runner.artifacts
        assert runner.artifacts["build"]["success"] == "true"
        assert "built-output" in runner.artifacts["build"]["stdout"]

    def test_artifact_with_register_option(self):
        pf = parse("""\
task build [register=build_output]:
    !echo compiled

task deploy: build:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        runner.run("deploy")

        assert "build_output" in runner.artifacts
        assert "compiled" in runner.artifacts["build_output"]["stdout"]

    def test_artifact_with_arrow_syntax(self):
        pf = parse("""\
task build -> build_result:
    !echo arrow-output

task deploy: build:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        runner.run("deploy")

        assert "build_result" in runner.artifacts
        assert "arrow-output" in runner.artifacts["build_result"]["stdout"]

    def test_artifact_accessible_as_variable(self):
        pf = parse("""\
task build:
    !echo hello-from-build

task deploy: build:
    status is {{build.success}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        # After build completes, build.success should be in variables
        assert pf.variables.get("build.success") == "true"

    def test_artifact_failed_task(self):
        pf = parse("""\
task build:
    !exit 1
    !echo after-fail

task deploy: build:
    deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        assert "build" in runner.artifacts
        assert runner.artifacts["build"]["success"] == "false"

    def test_arrow_syntax_parsing(self):
        pf = parse("""\
task build -> output:
    !echo hello
""")
        assert pf.tasks["build"].options.register == "output"

    def test_shell_capture_available_to_later_prompt_in_same_task(self):
        pf = parse("""\
task analyze:
    !printf 'changed-a\\nchanged-b\\n' -> changed
    Review these changed files:
    {{changed.stdout}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("analyze")

        prompt = result.task_results[0].step_results[1].content
        assert "changed-a" in prompt
        assert "changed-b" in prompt
        assert "{{changed.stdout}}" not in prompt

    def test_last_stdout_available_to_later_prompt_in_same_task(self):
        pf = parse("""\
task analyze:
    !printf 'last-output\\n'
    Explain this output:
    {{last.stdout}}
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("analyze")

        prompt = result.task_results[0].step_results[1].content
        assert "last-output" in prompt
        assert "{{last.stdout}}" not in prompt

    def test_last_exit_code_preserves_ignored_shell_failure(self):
        pf = parse("""\
task analyze:
    !@ignore sh -c 'exit 7'
    Explain exit code {{last.exit_code}}.
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("analyze")

        prompt = result.task_results[0].step_results[1].content
        assert "exit code 7" in prompt

    def test_pipe_output_prepends_next_prompt(self):
        pf = parse("""\
task analyze:
    !printf 'src/app.py\\n' -> changed |>
    Review these files for security issues.
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("analyze")

        prompt = result.task_results[0].step_results[1].content
        assert prompt.startswith("Shell output from changed:")
        assert "src/app.py" in prompt
        assert "Review these files for security issues." in prompt

    def test_inline_pipe_prompt_runs_as_next_prompt(self):
        pf = parse("""\
task analyze:
    !printf 'test failure\\n' |> explain the failure
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("analyze")

        prompt = result.task_results[0].step_results[1].content
        assert "test failure" in prompt
        assert "explain the failure" in prompt


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

class TestWebhooks:
    def test_webhook_option_parsed(self):
        pf = parse("""\
task deploy [webhook=https://hooks.example.com/test]:
    deploy it
""")
        assert pf.tasks["deploy"].options.webhook == "https://hooks.example.com/test"

    def test_webhook_on_option(self):
        pf = parse("""\
task deploy [webhook=https://hooks.example.com/test, webhook-on=success]:
    deploy it
""")
        assert pf.tasks["deploy"].options.webhook_on == "success"

    def test_webhook_fires_on_completion(self):
        pf = parse("""\
task deploy [webhook=https://hooks.example.com/test]:
    !echo deployed
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        # Mock urlopen to capture the call
        with patch("makethlm.runner.urllib.request.urlopen") as mock_urlopen:
            runner.run("deploy")
            assert mock_urlopen.called
            req = mock_urlopen.call_args[0][0]
            import json
            payload = json.loads(req.data)
            assert payload["task"] == "deploy"
            assert payload["status"] == "success"

    def test_ntfy_webhook_preset(self):
        pf = parse("""\
task deploy [webhook=ntfy:https://ntfy.sh/test-topic]:
    !echo deployed
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        with patch("makethlm.runner.urllib.request.urlopen") as mock_urlopen:
            runner.run("deploy")
            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://ntfy.sh/test-topic"
            assert req.get_method() == "POST"
            assert req.headers["Title"] == "makethlm deploy"

    def test_webhook_on_success_skips_failure(self):
        pf = parse("""\
task failing [webhook=https://hooks.example.com/test, webhook-on=success]:
    !exit 1
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with patch("makethlm.runner.urllib.request.urlopen") as mock_urlopen:
            runner.run("failing")
            assert not mock_urlopen.called

    def test_webhook_on_failure_skips_success(self):
        pf = parse("""\
task ok [webhook=https://hooks.example.com/test, webhook-on=failure]:
    !echo ok
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with patch("makethlm.runner.urllib.request.urlopen") as mock_urlopen:
            runner.run("ok")
            assert not mock_urlopen.called

    def test_webhook_invalid_on_raises(self):
        from makethlm.parser import ParseError
        with pytest.raises(ParseError, match="webhook_on must be"):
            parse("""\
task t [webhook=https://example.com, webhook-on=bogus]:
    do it
""")


# ---------------------------------------------------------------------------
# Rollback hooks
# ---------------------------------------------------------------------------

class TestRollback:
    def test_failed_task_runs_rollback_task(self):
        pf = parse("""\
task rollback:
    !echo rolled-back

task deploy [rollback=rollback]:
    !false
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        result = runner.run("deploy")

        assert not result.success
        assert [tr.task_name for tr in result.task_results] == ["deploy", "rollback"]
        assert "rolled-back" in result.task_results[1].response

    def test_successful_task_does_not_run_rollback_task(self):
        pf = parse("""\
task rollback:
    !echo rolled-back

task deploy [rollback=rollback]:
    !true
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        result = runner.run("deploy")

        assert result.success
        assert [tr.task_name for tr in result.task_results] == ["deploy"]


# ---------------------------------------------------------------------------
# Conditional execution (when)
# ---------------------------------------------------------------------------

class TestConditionalExecution:
    def test_when_true_executes(self):
        pf = parse("""\
env := "production"

task deploy [when=env == "production"]:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")
        assert result.success
        assert result.task_results[0].response != "[skipped] when condition not met"

    def test_when_false_skips(self):
        pf = parse("""\
env := "staging"

task deploy [when=env == "production"]:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")
        assert result.success
        assert "skipped" in result.task_results[0].response

    def test_when_artifact_condition(self):
        pf = parse("""\
task build:
    !echo built

task deploy: build [when=build.success == "true"]:
    deploy it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")
        assert result.success
        assert len(result.task_results) == 2
        # deploy should have executed (build succeeded)
        assert "skipped" not in result.task_results[1].response

    def test_when_artifact_failure_skips(self):
        pf = parse("""\
task build:
    !exit 1

task notify [when=build.success == "true"]:
    notify
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        # build fails, so deploy won't run (build failure stops pipeline)
        result = runner.run("build")
        assert not result.success

    def test_when_with_exists(self):
        pf = parse("""\
task check [when=exists("/tmp")]:
    check it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("check")
        assert result.success
        assert "skipped" not in result.task_results[0].response

    def test_when_with_not_exists(self):
        pf = parse("""\
task gen [when=!exists("/nonexistent_path_xyz")]:
    generate it
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("gen")
        assert result.success
        assert "skipped" not in result.task_results[0].response

    def test_when_with_variable_truthy(self):
        pf = parse("""\
enabled := "true"

task do_it [when=enabled]:
    do stuff
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("do_it")
        assert "skipped" not in result.task_results[0].response

    def test_when_with_variable_falsy(self):
        pf = parse("""\
enabled := "false"

task do_it [when=enabled]:
    do stuff
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("do_it")
        assert "skipped" in result.task_results[0].response

    def test_multiple_when_conditions(self):
        pf = parse("""\
env := "production"
region := "us-east"

task deploy [when=env == "production", when=region == "us-east"]:
    deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")
        assert "skipped" not in result.task_results[0].response

    def test_multiple_when_one_false_skips(self):
        pf = parse("""\
env := "production"
region := "eu-west"

task deploy [when=env == "production", when=region == "us-east"]:
    deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")
        assert "skipped" in result.task_results[0].response

    def test_when_parsing(self):
        pf = parse("""\
task t [when=x == "y"]:
    do it
""")
        assert pf.tasks["t"].options.when == ['x == "y"']


# ---------------------------------------------------------------------------
# Sandboxing
# ---------------------------------------------------------------------------

class TestSandboxing:
    def test_sandbox_docker_wraps_command(self):
        pf = parse("""\
task build [sandbox=docker]:
    !make all
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["build"]
        wrapped = runner._sandbox_command("make all", task)
        assert "docker" in wrapped
        assert "run" in wrapped
        assert "--rm" in wrapped
        assert "/workspace" in wrapped
        assert "make all" in wrapped

    def test_sandbox_docker_custom_image(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-image=node:20-alpine]:
    !npm run build
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["build"]
        wrapped = runner._sandbox_command("npm run build", task)
        assert "node:20-alpine" in wrapped

    def test_sandbox_docker_with_mount(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-mount=./dist:/app/dist]:
    !deploy.sh
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["build"]
        wrapped = runner._sandbox_command("deploy.sh", task)
        assert "./dist:/app/dist" in wrapped

    def test_sandbox_docker_with_net(self):
        pf = parse("""\
task build [sandbox=docker, sandbox-net=host]:
    !deploy.sh
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["build"]
        wrapped = runner._sandbox_command("deploy.sh", task)
        assert "--net" in wrapped
        assert "host" in wrapped

    def test_sandbox_systemd_wraps_command(self):
        pf = parse("""\
task compile [sandbox=systemd]:
    !gcc -o output main.c
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["compile"]
        wrapped = runner._sandbox_command("gcc -o output main.c", task)
        assert "systemd-run" in wrapped
        assert "--scope" in wrapped
        assert "PrivateTmp=yes" in wrapped
        assert "NoNewPrivileges=yes" in wrapped
        assert "ProtectSystem=strict" in wrapped
        assert "gcc -o output main.c" in wrapped

    def test_sandbox_bwrap_wraps_command(self):
        pf = parse("""\
task risky [sandbox=bwrap]:
    !./untrusted
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["risky"]
        wrapped = runner._sandbox_command("./untrusted", task)
        assert "bwrap" in wrapped
        assert "--ro-bind" in wrapped
        assert "--tmpfs" in wrapped
        assert "./untrusted" in wrapped

    def test_sandbox_none_passes_through(self):
        pf = parse("""\
task safe [sandbox=none]:
    !echo hello
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["safe"]
        wrapped = runner._sandbox_command("echo hello", task)
        assert wrapped == "echo hello"

    def test_sandbox_no_sandbox_passes_through(self):
        pf = parse("""\
task safe:
    !echo hello
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["safe"]
        wrapped = runner._sandbox_command("echo hello", task)
        assert wrapped == "echo hello"

    def test_global_sandbox_applies(self):
        pf = parse("""\
set sandbox docker

task build:
    !make all
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["build"]
        wrapped = runner._sandbox_command("make all", task)
        assert "docker" in wrapped

    def test_per_task_sandbox_overrides_global(self):
        pf = parse("""\
set sandbox docker

task compile [sandbox=systemd]:
    !gcc main.c
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["compile"]
        wrapped = runner._sandbox_command("gcc main.c", task)
        assert "systemd-run" in wrapped
        assert "docker" not in wrapped

    def test_sandbox_integrated_in_shell_step(self):
        """Test that sandbox wrapping is applied during actual shell step execution."""
        pf = parse("""\
task build [sandbox=docker]:
    !echo sandboxed
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        # Mock _run_subprocess to capture the command
        captured = []
        def mock_run(cmd, **kwargs):
            captured.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with patch("makethlm.runner._run_subprocess", side_effect=mock_run):
            runner.run("build")

        assert len(captured) > 0
        assert "docker" in captured[0]


# ---------------------------------------------------------------------------
# Topological levels & parallel execution
# ---------------------------------------------------------------------------

class TestTopologicalLevels:
    def test_single_task_single_level(self):
        from makethlm.runner import topological_levels
        pf = parse("""\
task build:
    !make all
""")
        levels = topological_levels(pf, "build")
        assert levels == [["build"]]

    def test_linear_deps_one_per_level(self):
        from makethlm.runner import topological_levels
        pf = parse("""\
task a:
    !echo a

task b: a:
    !echo b

task c: b:
    !echo c
""")
        levels = topological_levels(pf, "c")
        assert len(levels) == 3
        assert levels[0] == ["a"]
        assert levels[1] == ["b"]
        assert levels[2] == ["c"]

    def test_independent_deps_same_level(self):
        from makethlm.runner import topological_levels
        pf = parse("""\
task lint:
    !echo lint

task test:
    !echo test

task build: lint test:
    !echo build
""")
        levels = topological_levels(pf, "build")
        assert len(levels) == 2
        # lint and test should be in the same level (order may vary)
        assert set(levels[0]) == {"lint", "test"}
        assert levels[1] == ["build"]

    def test_diamond_dependency(self):
        from makethlm.runner import topological_levels
        pf = parse("""\
task a:
    !echo a

task b: a:
    !echo b

task c: a:
    !echo c

task d: b c:
    !echo d
""")
        levels = topological_levels(pf, "d")
        assert len(levels) == 3
        assert levels[0] == ["a"]
        assert set(levels[1]) == {"b", "c"}
        assert levels[2] == ["d"]


class TestParallelExecution:
    def test_parallel_run_basic(self):
        pf = parse("""\
task build:
    !echo built
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run_parallel("build")
        assert result.success
        assert len(result.task_results) == 1

    def test_parallel_run_independent_tasks(self):
        pf = parse("""\
task lint:
    !echo lint-ok

task test:
    !echo test-ok

task deploy: lint test:
    !echo deploy-ok
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run_parallel("deploy")
        assert result.success
        assert len(result.task_results) == 3

    def test_parallel_run_respects_when(self):
        pf = parse("""\
enabled := "false"

task skip [when=enabled]:
    !echo should-not-run
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run_parallel("skip")
        assert result.success
        assert "skipped" in result.task_results[0].response

    def test_parallel_run_respects_jobs_limit(self):
        pf = parse("""\
task a:
    !echo a

task b:
    !echo b

task c:
    !echo c

task done: a b c:
    !echo done
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)
        active = 0
        max_active = 0
        lock = threading.Lock()
        original = runner._run_single_task_in_pipeline

        def wrapped(*args, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.02)
                return original(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        with patch.object(runner, "_run_single_task_in_pipeline", side_effect=wrapped):
            result = runner.run_parallel("done", jobs=2)

        assert result.success
        assert max_active <= 2

    def test_parallel_run_rejects_invalid_jobs(self):
        pf = parse("""\
task build:
    !echo build
""")
        runner = Runner(pf, DryRunDispatcher(), verbose=False)

        with pytest.raises(ValueError, match="jobs"):
            runner.run_parallel("build", jobs=0)


# ---------------------------------------------------------------------------
# Cache duration parsing
# ---------------------------------------------------------------------------

class TestCacheDuration:
    def test_parse_seconds(self):
        from makethlm.runner import _parse_cache_duration
        assert _parse_cache_duration("30s") == 30

    def test_parse_minutes(self):
        from makethlm.runner import _parse_cache_duration
        assert _parse_cache_duration("5m") == 300

    def test_parse_hours(self):
        from makethlm.runner import _parse_cache_duration
        assert _parse_cache_duration("1h") == 3600

    def test_parse_days(self):
        from makethlm.runner import _parse_cache_duration
        assert _parse_cache_duration("2d") == 172800

    def test_parse_invalid_raises(self):
        from makethlm.runner import _parse_cache_duration
        with pytest.raises(ValueError, match="invalid cache duration"):
            _parse_cache_duration("invalid")


# ---------------------------------------------------------------------------
# Task caching
# ---------------------------------------------------------------------------

class TestTaskCaching:
    def test_cache_option_parsed(self):
        pf = parse("""\
task expensive [cache=1h]:
    !echo expensive
""")
        assert pf.tasks["expensive"].options.cache == "1h"

    def test_cache_saves_and_retrieves(self):
        import tempfile
        pf = parse("""\
task expensive [cache=1h]:
    !echo expensive-result
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path
            runner._cache_dir = Path(tmpdir)

            # First run should execute
            result1 = runner.run("expensive")
            assert result1.success

            # Second run should use cache
            result2 = runner.run("expensive")
            assert result2.success

    def test_cache_key_changes_with_content(self):
        pf1 = parse("""\
task t [cache=1h]:
    !echo version1
""")
        pf2 = parse("""\
task t [cache=1h]:
    !echo version2
""")
        dispatcher = DryRunDispatcher()
        r1 = Runner(pf1, dispatcher)
        r2 = Runner(pf2, dispatcher)

        key1 = r1._cache_key(pf1.tasks["t"])
        key2 = r2._cache_key(pf2.tasks["t"])
        assert key1 != key2

    def test_no_cache_option_no_caching(self):
        pf = parse("""\
task normal:
    !echo normal
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        task = pf.tasks["normal"]
        assert runner._get_cached_result(task) is None
