"""Tests for CLI-only output modes."""

import sqlite3

from makethlm.cli import main


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
