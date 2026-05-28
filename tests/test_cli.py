"""Tests for CLI-only output modes."""

import json
import sqlite3
from unittest.mock import patch

from makethlm.cli import main
from makethlm.runner import RunResult


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

    with patch("makethlm.runner.Runner.run_parallel", return_value=RunResult(target="build")) as run_parallel:
        code = main(["-f", str(promptfile), "--dry-run", "--parallel", "build"])

    assert code == 0
    run_parallel.assert_called_once()


def test_jobs_implies_parallel_and_passes_limit(tmp_path, capsys):
    promptfile = tmp_path / "Promptfile"
    promptfile.write_text("""\
task build:
    !echo build
""")

    with patch("makethlm.runner.Runner.run_parallel", return_value=RunResult(target="build")) as run_parallel:
        code = main(["-f", str(promptfile), "--dry-run", "--jobs", "3", "build"])

    assert code == 0
    assert run_parallel.call_args.kwargs["jobs"] == 3


def test_jobs_must_be_positive(capsys):
    code = main(["--jobs", "0"])
    err = capsys.readouterr().err

    assert code == 1
    assert "--jobs must be at least 1" in err


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
