"""The check entry point must retain coverage and refuse implicit database targets."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check.py"
SPEC = importlib.util.spec_from_file_location("coinpup_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
checks = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checks
SPEC.loader.exec_module(checks)


def test_default_fast_covers_python_contracts_and_offline_migration(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    commands = []

    def run(command, **options):
        commands.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks.main([]) == 0
    assert [command[1:] for command, _ in commands] == [
        ["-m", "ruff", "check", "."],
        ["-m", "ruff", "format", "--check", "."],
        ["-m", "pytest", "-m", "not integration"],
        ["scripts/check_docs.py"],
        ["scripts/export_openapi.py", "--check"],
        ["-m", "alembic", "upgrade", "head", "--sql"],
    ]
    assert all(command[0] == sys.executable for command, _ in commands)
    assert all(options["cwd"] == tmp_path for _, options in commands)
    assert commands[-1][1]["stdout"] == subprocess.DEVNULL
    assert "6 passed, 0 failed, 1 skipped" in capsys.readouterr().out


def test_fast_runs_web_checks_when_dependencies_exist(monkeypatch, tmp_path):
    (tmp_path / "apps/web/node_modules").mkdir(parents=True)
    monkeypatch.setattr(checks.shutil, "which", lambda name: "fictional-npm.cmd")
    steps, skipped = checks.build_steps("fast", False, tmp_path)
    assert skipped == []
    assert [step.command for step in steps[-2:]] == [
        ["fictional-npm.cmd", "--prefix", "apps/web", "run", "typecheck"],
        ["fictional-npm.cmd", "--prefix", "apps/web", "run", "test:unit"],
    ]


def test_web_includes_build_and_uses_npm_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        checks.shutil,
        "which",
        lambda name: {"npm": "fictional-npm.cmd", "git": "fictional-git.exe"}.get(name),
    )
    steps, skipped = checks.build_steps("web", False, tmp_path)
    assert skipped == []
    assert [step.command for step in steps[:2]] == [
        ["fictional-npm.cmd", "--prefix", "apps/web", "run", "gen:api"],
        ["fictional-git.exe", "diff", "--exit-code", "apps/web/src/generated"],
    ]
    assert [step.command[-1] for step in steps[2:]] == [
        "format:check",
        "lint",
        "typecheck",
        "test:unit",
        "build",
    ]
    assert all(step.command[0] == "fictional-npm.cmd" for step in steps[2:])


@pytest.mark.parametrize("mode", ["fast", "web"])
def test_missing_npm_is_a_failure_when_web_checks_are_required(monkeypatch, tmp_path, mode):
    (tmp_path / "apps/web/node_modules").mkdir(parents=True)
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        checks.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run")
    )
    assert checks.main([mode]) != 0


@pytest.mark.parametrize(
    "enabled,url", [(None, None), ("1", None), ("0", "fictional-url"), ("1", " ")]
)
def test_database_requires_both_explicit_opt_ins_before_any_checks(monkeypatch, enabled, url):
    monkeypatch.delenv("COINPUP_RUN_DB_TESTS", raising=False)
    monkeypatch.delenv("COINPUP_DATABASE_URL", raising=False)
    if enabled is not None:
        monkeypatch.setenv("COINPUP_RUN_DB_TESTS", enabled)
    if url is not None:
        monkeypatch.setenv("COINPUP_DATABASE_URL", url)
    monkeypatch.setattr(
        checks.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run")
    )
    assert checks.main(["db", "--fix"]) != 0


def test_database_warns_without_disclosing_url_and_preserves_round_trip(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    monkeypatch.setenv("COINPUP_RUN_DB_TESTS", "1")
    private_url = "postgresql+psycopg://fictional:never-print-this@localhost/disposable"
    monkeypatch.setenv("COINPUP_DATABASE_URL", private_url)
    commands = []

    def run(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks.main(["db"]) == 0
    assert [command[1:] for command in commands] == [
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "downgrade", "base"],
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "check"],
        ["-m", "pytest", "tests/integration", "-m", "integration"],
    ]
    output = capsys.readouterr().out
    assert "ONLY" in output and "disposable test database" in output
    assert output.index("WARNING:") < output.index("[RUN]")
    assert private_url not in output and "never-print-this" not in output


def test_failure_stops_following_checks_and_returns_failure_code(monkeypatch, tmp_path, capsys):
    commands = []

    def run(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(checks.subprocess, "run", run)
    steps = [checks.Step("First check", ["first"]), checks.Step("Must not run", ["second"])]
    assert checks.run_steps(steps, tmp_path, []) == 7
    assert commands == [["first"]]
    output = capsys.readouterr().out
    assert "0 passed, 1 failed" in output and "Failed check: First check" in output


def test_tool_launch_failure_is_reported_as_failure(monkeypatch, tmp_path, capsys):
    def run(*args, **kwargs):
        raise FileNotFoundError("fictional missing tool")

    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks.run_steps([checks.Step("Missing tool", ["missing"])], tmp_path, []) == 1
    assert "[FAIL] Missing tool" in capsys.readouterr().out


def test_fix_runs_before_checks_without_removing_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(checks.shutil, "which", lambda name: "fictional-npm.cmd")
    steps, skipped = checks.build_steps("fast", True, tmp_path)
    assert steps[0].command == [sys.executable, "-m", "ruff", "check", "--fix", "."]
    assert steps[1].command == [sys.executable, "-m", "ruff", "format", "."]
    assert any(step.command[1:] == ["-m", "ruff", "format", "--check", "."] for step in steps)
    assert any("--sql" in step.command for step in steps)
    assert skipped


@pytest.mark.parametrize("mode", ["fast", "web"])
def test_fix_formats_web_before_checks_when_web_checks_run(monkeypatch, tmp_path, mode):
    (tmp_path / "apps/web/node_modules").mkdir(parents=True)
    monkeypatch.setattr(
        checks.shutil,
        "which",
        lambda name: {"npm": "fictional-npm.cmd", "git": "fictional-git.exe"}.get(name),
    )
    steps, skipped = checks.build_steps(mode, True, tmp_path)
    assert skipped == []
    assert [step.command for step in steps[:3]] == [
        [sys.executable, "-m", "ruff", "check", "--fix", "."],
        [sys.executable, "-m", "ruff", "format", "."],
        ["fictional-npm.cmd", "--prefix", "apps/web", "run", "format"],
    ]
    if mode == "fast":
        assert steps[3].command == [sys.executable, "-m", "ruff", "check", "."]
        assert [step.command[-1] for step in steps[-2:]] == ["typecheck", "test:unit"]
    else:
        assert [step.command for step in steps[3:5]] == [
            ["fictional-npm.cmd", "--prefix", "apps/web", "run", "gen:api"],
            ["fictional-git.exe", "diff", "--exit-code", "apps/web/src/generated"],
        ]
        assert [step.command[-1] for step in steps[5:]] == [
            "format:check",
            "lint",
            "typecheck",
            "test:unit",
            "build",
        ]


def test_database_fix_preserves_database_checks_without_discovering_npm(monkeypatch, tmp_path):
    (tmp_path / "apps/web/node_modules").mkdir(parents=True)
    monkeypatch.setattr(checks.shutil, "which", lambda name: pytest.fail("db must not need npm"))
    steps, skipped = checks.build_steps("db", True, tmp_path)
    assert skipped == []
    assert all(step.command[0] == sys.executable for step in steps)
    assert [step.command[1:] for step in steps] == [
        ["-m", "ruff", "check", "--fix", "."],
        ["-m", "ruff", "format", "."],
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "downgrade", "base"],
        ["-m", "alembic", "upgrade", "head"],
        ["-m", "alembic", "check"],
        ["-m", "pytest", "tests/integration", "-m", "integration"],
    ]


def test_missing_git_blocks_web_checks_before_any_execution(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    monkeypatch.setattr(
        checks.shutil, "which", lambda name: "fictional-npm.cmd" if name == "npm" else None
    )
    monkeypatch.setattr(
        checks.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run")
    )
    assert checks.main(["web", "--fix"]) != 0
    assert "git was not found on PATH" in capsys.readouterr().err


def test_changed_generated_types_fail_before_lint_or_build(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, "ROOT", tmp_path)
    monkeypatch.setattr(
        checks.shutil,
        "which",
        lambda name: {"npm": "fictional-npm.cmd", "git": "fictional-git.exe"}.get(name),
    )
    commands = []

    def run(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(command, int(command[0] == "fictional-git.exe"))

    monkeypatch.setattr(checks.subprocess, "run", run)
    assert checks.main(["web"]) == 1
    assert commands == [
        ["fictional-npm.cmd", "--prefix", "apps/web", "run", "gen:api"],
        ["fictional-git.exe", "diff", "--exit-code", "apps/web/src/generated"],
    ]
