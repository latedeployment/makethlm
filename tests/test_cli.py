"""Tests for CLI-only output modes."""

import json
import sqlite3
import stat
from unittest.mock import patch

from makethlm.cli import _capability_payload, _validate_safe_mode, _validate_tools, main
from makethlm.dispatcher import ClaudeDispatcher, OpenAIDispatcher
from makethlm.history import get_run, record_run
from makethlm.parser import parse
from makethlm.runner import RunResult, StepResult, TaskResult


def test_plan_outputs_execution_details(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
project := "demo"

hosts web:
    web1
    web2

task build:
    !echo build {{project}}

task deploy(env): build [on=web, timeout=30s, llm-timeout=2m, ssh-parallel]:
    !echo deploy {{env}}
    verify {{project}} on {{env}}
""")

    code = main(["-f", str(promptfile), "--plan", "deploy", "staging"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Plan for deploy" in out
    assert "project='demo'" in out
    assert "1. build" in out
    assert "2. deploy" in out
    assert "hosts: web (2 hosts), parallel" in out
    assert "options: timeout=30s, llm-timeout=2m" in out
    assert "! echo deploy staging" in out
    assert "> verify demo on staging" in out


def test_list_outputs_modules_aliases_and_attributes(tmp_path, capsys):
    module = tmp_path / "ops.pf"
    module.write_text("""\
task deploy(env) [timeout=30s, sandbox=docker, ssh-parallel]:
    deploy service
""")
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
mod ops "ops.pf"

alias d := ops::deploy
""")

    code = main(["-f", str(promptfile), "--list"])
    out = capsys.readouterr().out

    assert code == 0
    assert "modules:" in out
    assert "[ops]" in out
    assert "ops::deploy" in out
    assert "aliases: d" in out
    assert "attrs: timeout=30s, sandbox=docker, ssh-parallel" in out
    assert "d -> ops::deploy" in out


def test_list_outputs_json_payload(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
alias b := build

task build [cache=10m]:
    build project
""")

    code = main(["-f", str(promptfile), "--list", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["aliases"] == {"b": "build"}
    assert payload["tasks"][0]["name"] == "build"
    assert payload["tasks"][0]["aliases"] == ["b"]
    assert payload["tasks"][0]["attributes"] == ["cache=10m"]


def test_plan_masks_secret_placeholders(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(tmp_path / "history.sqlite"))
    monkeypatch.setenv("PLAN_SECRET", "supersecret")
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
set secrets "env"

task review:
    !echo {{#secret:PLAN_SECRET}}
    review {{#secret:PLAN_SECRET}}
""")

    code = main(["-f", str(promptfile), "--plan", "review"])
    out = capsys.readouterr().out

    assert code == 0
    assert "supersecret" not in out
    assert "***" in out


def test_plan_outputs_json(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
project := "demo"

task build:
    !echo {{project}}
""")

    code = main(["-f", str(promptfile), "--plan", "--json", "build"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["target"] == "build"
    assert payload["variables"] == {"project": "demo"}
    assert payload["execution_order"] == ["build"]
    assert payload["tasks"][0]["steps"][0] == {"kind": "shell", "content": "echo demo"}


def test_multiple_task_invocation_runs_each_task(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
first:
    echo first

second:
    echo second
""")

    code = main(["-f", str(promptfile), "--dry-run", "first", "second"])
    out = capsys.readouterr().out

    assert code == 0
    assert "[ok] first" in out
    assert "[ok] second" in out


def test_graph_outputs_mermaid_for_target(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    build

task test: build:
    test

task deploy: test:
    deploy
""")

    code = main(["-f", str(promptfile), "--graph", "deploy"])
    out = capsys.readouterr().out

    assert code == 0
    assert "graph TD" in out
    assert "task_build --> task_test" in out
    assert "task_test --> task_deploy" in out


def test_graph_outputs_dot(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    build

task deploy: build:
    deploy
""")

    code = main(["-f", str(promptfile), "--graph", "--graph-format", "dot"])
    out = capsys.readouterr().out

    assert code == 0
    assert "digraph makethlm" in out
    assert '"build" -> "deploy";' in out


def test_graph_does_not_require_task_runtime_args(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    build

task deploy(env): build:
    deploy {{env}}
""")

    code = main(["-f", str(promptfile), "--graph", "deploy"])
    out = capsys.readouterr().out

    assert code == 0
    assert "task_build --> task_deploy" in out


def test_graph_outputs_json(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    build

task deploy: build:
    deploy
""")

    code = main(["-f", str(promptfile), "--graph", "--json", "deploy"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["target"] == "deploy"
    assert payload["nodes"] == ["build", "deploy"]
    assert payload["edges"] == [{"from": "build", "to": "deploy"}]


def test_history_command_outputs_recorded_runs(tmp_path, capsys, monkeypatch):
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                target TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                promptfile TEXT,
                task_count INTEGER NOT NULL,
                tasks_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO runs (
                started_at, target, success, duration_ms, promptfile, task_count, tasks_json
            ) VALUES ('2026-01-01T00:00:00Z', 'deploy', 1, 12, 'Promptfile', 1, '[]')
            """
        )

    code = main(["history"])
    out = capsys.readouterr().out

    assert code == 0
    assert "deploy" in out
    assert "ok" in out


def test_history_outputs_json(tmp_path, capsys, monkeypatch):
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                target TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                promptfile TEXT,
                task_count INTEGER NOT NULL,
                tasks_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO runs (
                started_at, target, success, duration_ms, promptfile, task_count, tasks_json
            ) VALUES (
                '2026-01-01T00:00:00Z', 'deploy', 1, 12, 'Promptfile', 1,
                '[{"task": "deploy", "success": true}]'
            )
            """
        )

    code = main(["--json", "history"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["runs"][0]["target"] == "deploy"
    assert payload["runs"][0]["success"] is True
    assert payload["runs"][0]["tasks"][0]["task"] == "deploy"


def test_replay_outputs_recorded_run_bundle(tmp_path, capsys, monkeypatch):
    db = tmp_path / "history.sqlite"
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !printf 'built'
""")
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(db))

    assert main(["-f", str(promptfile), "build"]) == 0
    capsys.readouterr()

    code = main(["--json", "replay", "1"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["schema"] == 1
    assert payload["target"] == "build"
    assert payload["tasks"][0]["response"] == "built"
    assert payload["tasks"][0]["steps"][0]["content"] == "printf 'built'"


def test_history_redacts_environment_secrets_and_uses_private_permissions(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("DEPLOY_TOKEN", "history-secret-value")
    monkeypatch.setenv("PASSWORD", "xQz")
    result = RunResult(
        target="deploy",
        task_results=[
            TaskResult(
                task_name="deploy",
                prompt_sent="use history-secret-value and xQz",
                response="history-secret-value xQz",
                success=True,
                step_results=[
                    StepResult(
                        kind="prompt",
                        content="use history-secret-value and xQz",
                        response="history-secret-value xQz",
                        success=True,
                    )
                ],
            )
        ],
    )

    run_id = record_run(
        result,
        duration_ms=1,
        promptfile_path="Promptfile",
        path=db,
    )
    bundle = get_run(run_id, path=db)

    assert bundle is not None
    assert "history-secret-value" not in json.dumps(bundle)
    assert "xQz" not in json.dumps(bundle)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_capabilities_explains_failure_hook_closure(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task diagnose:
    diagnose

task undo:
    !echo undo

task deploy [postmortem=diagnose, rollback=undo, webhook=https://example.test/hook]:
    !echo deploy
""")

    code = main(
        [
            "-f",
            str(promptfile),
            "--json",
            "--capabilities",
            "deploy",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["tasks"] == ["deploy", "diagnose", "undo"]
    assert payload["required"] == ["llm", "shell", "webhook"]


def test_capabilities_does_not_execute_parse_time_backticks(tmp_path, capsys):
    marker = tmp_path / "executed"
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text(f"""\
value := `touch {marker}`

task show:
    {{{{value}}}}
""")

    code = main(["-f", str(promptfile), "--capabilities", "show"])

    assert code == 1
    assert "backtick command substitution is disabled" in capsys.readouterr().err
    assert not marker.exists()


def test_safe_mode_docker_block_also_requires_llm_permission():
    pf = parse("""\
docker image:
    create a minimal image
""")
    errors = _validate_safe_mode(
        pf,
        "image",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=True,
        allow_llm=False,
    )
    assert any("--allow-llm" in error for error in errors)


def test_safe_mode_requires_shell_for_local_execution_llm():
    pf = parse("""\
task review:
    review the project
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
    )

    assert any("local execution access" in error for error in errors)


def test_safe_mode_native_api_llm_does_not_require_shell():
    pf = parse("""\
task review:
    review the project
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
        dispatcher=OpenAIDispatcher(api_key="test"),
    )

    assert not any("--allow-shell" in error for error in errors)


def test_safe_mode_fallback_local_llm_requires_shell():
    pf = parse("""\
llm openai [model=gpt-test]
llm claude [model=sonnet]

task review [llm=openai, fallback-llm=claude]:
    review the project
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
        dispatcher=OpenAIDispatcher(api_key="test"),
    )

    assert any("local execution access" in error for error in errors)


def test_safe_mode_requires_explicit_secret_access_for_expanded_function(monkeypatch):
    monkeypatch.setenv("DEPLOY_TOKEN", "sensitive-token")
    pf = parse("""\
llm openai [model=gpt-test]
secret_prompt := "{{#secret:DEPLOY_TOKEN}}"

fn credentials:
    use {{secret_prompt}}

task review [llm=openai]:
    @use credentials
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
        allow_secrets=False,
        dispatcher=OpenAIDispatcher(api_key="test"),
    )

    assert any("--allow-secrets" in error for error in errors)


def test_safe_mode_detects_secret_environment_interpolation(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sensitive-token")
    pf = parse("""\
llm openai [model=gpt-test]

task review [llm=openai]:
    inspect ${AWS_SECRET_ACCESS_KEY} and {{env_var("AWS_SECRET_ACCESS_KEY")}}
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
        allow_secrets=False,
        dispatcher=OpenAIDispatcher(api_key="test"),
    )

    assert any("--allow-secrets" in error for error in errors)


def test_safe_mode_permissions_every_explicit_environment_read(monkeypatch):
    monkeypatch.setenv("CI_JOB_JWT", "credential-value")
    pf = parse("""\
llm openai [model=gpt-test]

task review [llm=openai]:
    inspect {{env_var("CI_JOB_JWT")}}
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
        allow_secrets=False,
        dispatcher=OpenAIDispatcher(api_key="test"),
    )

    assert any("--allow-secrets" in error for error in errors)


def test_safe_mode_detects_transformed_secret_variable():
    pf = parse("""\
llm openai [model=gpt-test]
PASSWORD := "sensitive-token"

task review [llm=openai]:
    inspect {{uppercase(PASSWORD)}}
""")

    errors = _validate_safe_mode(
        pf,
        "review",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
        allow_secrets=False,
        dispatcher=OpenAIDispatcher(api_key="test"),
    )

    assert any("--allow-secrets" in error for error in errors)


def test_capabilities_discloses_secrets_and_hides_webhook_url():
    pf = parse("""\
llm openai [model=gpt-test]

task notify [llm=openai, webhook=https://token@example.test/private]:
    use {{#secret:DEPLOY_TOKEN}}
""")

    payload = _capability_payload(
        pf,
        "notify",
        dispatcher=OpenAIDispatcher(api_key="test"),
    )
    serialized = json.dumps(payload)

    assert "secrets" in payload["required"]
    assert "--allow-secrets" in serialized
    assert "DEPLOY_TOKEN" in serialized
    assert "token@example.test" not in serialized


def test_safe_mode_blocks_shell_without_permission(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !echo build
""")

    code = main(["-f", str(promptfile), "--safe", "--shell", "true", "build"])
    err = capsys.readouterr().err

    assert code == 1
    assert "safe mode blocked execution" in err
    assert "--allow-shell" in err


def test_safe_mode_allows_shell_with_permission(tmp_path, capsys, monkeypatch):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !echo build
""")
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(tmp_path / "history.sqlite"))

    code = main(["-f", str(promptfile), "--safe", "--allow-shell", "--shell", "true", "build"])
    out = capsys.readouterr().out

    assert code == 0
    assert "[ok] build" in out


def test_dry_run_does_not_execute_shell_steps(tmp_path, capsys):
    marker = tmp_path / "marker"
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text(f"""\
task build:
    !touch {marker}
""")

    code = main(["-f", str(promptfile), "--dry-run", "build"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"! touch {marker}" in out
    assert not marker.exists()


def test_run_outputs_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(tmp_path / "history.sqlite"))
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !echo build
""")

    code = main(["-f", str(promptfile), "--json", "build"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["target"] == "build"
    assert payload["success"] is True
    assert payload["exit_code"] == 0
    assert payload["run_id"] == 1
    assert payload["tasks"][0]["task"] == "build"
    assert payload["tasks"][0]["steps"][0]["kind"] == "shell"
    assert payload["tasks"][0]["steps"][0]["exit_code"] == 0


def test_json_output_redacts_secret_step_content(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MAKETHLM_HISTORY_DB", str(tmp_path / "history.sqlite"))
    monkeypatch.setenv("JSON_SECRET", "supersecret")
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
set secrets "env"

task show:
    !echo {{#secret:JSON_SECRET}}
""")

    code = main(["-f", str(promptfile), "--json", "show"])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert code == 0
    assert "supersecret" not in out
    assert "[redacted]" in payload["tasks"][0]["steps"][0]["content"]


def test_parallel_flag_uses_parallel_runner(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !echo build
""")

    with patch(
        "makethlm.runner.Runner.run_parallel", return_value=RunResult(target="build")
    ) as run_parallel:
        code = main(["-f", str(promptfile), "--dry-run", "--parallel", "build"])

    assert code == 0
    run_parallel.assert_called_once()


def test_jobs_implies_parallel_and_passes_limit(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !echo build
""")

    with patch(
        "makethlm.runner.Runner.run_parallel", return_value=RunResult(target="build")
    ) as run_parallel:
        code = main(["-f", str(promptfile), "--dry-run", "--jobs", "3", "build"])

    assert code == 0
    assert run_parallel.call_args.kwargs["jobs"] == 3


def test_jobs_must_be_positive(capsys):
    code = main(["--jobs", "0"])
    err = capsys.readouterr().err

    assert code == 1
    assert "--jobs must be at least 1" in err


def test_completions_outputs_shell_script(capsys):
    code = main(["completions", "bash"])
    out = capsys.readouterr().out

    assert code == 0
    assert "complete -F _makethlm_complete makethlm" in out


def test_completions_rejects_unknown_shell(capsys):
    code = main(["completions", "powershell"])
    err = capsys.readouterr().err

    assert code == 1
    assert "unsupported shell" in err


def test_check_succeeds_for_shell_only_promptfile(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
build:
    echo build
""")

    code = main(["-f", str(promptfile), "--check"])
    out = capsys.readouterr().out

    assert code == 0
    assert "OK:" in out


def test_check_outputs_json(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
build:
    echo build
""")

    code = main(["-f", str(promptfile), "--check", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["summary"]["tasks"] == 1


def test_check_reports_unknown_llm_provider(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task review [llm=missing]:
    review this
""")

    code = main(["-f", str(promptfile), "--check"])
    out = capsys.readouterr().out

    assert code == 1
    assert "unknown-provider" in out
    assert "missing" in out


def test_check_blocks_backticks_by_default(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
version := `echo 1`

build:
    echo {{version}}
""")

    code = main(["-f", str(promptfile), "--check"])
    err = capsys.readouterr().err

    assert code == 1
    assert "backtick command substitution is disabled" in err


def test_safe_mode_validates_rollback_task_capabilities():
    pf = parse("""\
task cleanup:
    !echo cleanup

task deploy [rollback=cleanup]:
    deploy
""")

    errors = _validate_safe_mode(
        pf,
        "deploy",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=True,
    )

    assert any("cleanup" in error and "--allow-shell" in error for error in errors)


def test_safe_mode_blocks_shell_backtick_conditions():
    pf = parse("""\
task guarded [when=`true`]:
    @echo guarded
""")

    errors = _validate_safe_mode(
        pf,
        "guarded",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=False,
    )

    assert any("shell condition" in error for error in errors)


def test_safe_mode_blocks_negated_shell_backtick_conditions():
    pf = parse("""\
task guarded [when=!`false`]:
    @echo guarded
""")

    errors = _validate_safe_mode(
        pf,
        "guarded",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=False,
    )

    assert any("shell condition" in error for error in errors)


def test_safe_mode_blocks_webhooks_without_permission():
    pf = parse("""\
task notify [webhook=https://example.invalid/hook]:
    @echo done
""")

    errors = _validate_safe_mode(
        pf,
        "notify",
        allow_shell=False,
        allow_ssh=False,
        allow_docker=False,
        allow_llm=False,
    )

    assert any("--allow-webhook" in error for error in errors)


def test_shell_only_task_does_not_validate_default_llm_tool():
    pf = parse("""\
build:
    echo build
""")

    with patch("makethlm.dispatcher.shutil.which", return_value=None):
        errors = _validate_tools(ClaudeDispatcher(), pf, "build")

    assert errors == []


def test_plan_redacts_secret_named_variables(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
export API_KEY := "top-secret-value"

task show:
    !echo {{API_KEY}}
""")

    code = main(["-f", str(promptfile), "--plan", "show"])
    output = capsys.readouterr().out

    assert code == 0
    assert "top-secret-value" not in output
    assert "[redacted]" in output


def test_plan_redacts_short_secret_named_variables(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
PASSWORD := "xQz"

task show:
    !echo {{PASSWORD}}
""")

    code = main(["-f", str(promptfile), "--plan", "show"])
    output = capsys.readouterr().out

    assert code == 0
    assert "xQz" not in output
    assert "[redacted]" in output


def test_plan_redacts_secret_task_arguments(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task show(PASSWORD):
    !echo {{PASSWORD}}
""")

    code = main(["-f", str(promptfile), "--plan", "show", "123"])
    output = capsys.readouterr().out

    assert code == 0
    assert "123" not in output
    assert "[redacted]" in output


def test_list_and_dump_redact_secret_argument_defaults(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task show(password="default-secret"):
    !echo ready
""")

    list_code = main(["-f", str(promptfile), "--list"])
    list_output = capsys.readouterr().out
    dump_code = main(["-f", str(promptfile), "--dump"])
    dump_output = capsys.readouterr().out

    assert list_code == 0
    assert dump_code == 0
    assert "default-secret" not in list_output
    assert "default-secret" not in dump_output
    assert "[redacted]" in list_output
    assert "[redacted]" in dump_output


def test_cli_rejects_unexpected_task_arguments(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task show(value):
    show {{value}}
""")

    code = main(["-f", str(promptfile), "--dry-run", "show", "one", "unexpected"])
    error = capsys.readouterr().err

    assert code == 1
    assert "unexpected arguments" in error
