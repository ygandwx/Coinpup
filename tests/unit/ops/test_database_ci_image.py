"""Disposable database startup keeps the pinned image, fails closed and cleans its own name."""

import subprocess
from types import SimpleNamespace

import pytest

from scripts.prepare_browser_ci_image import POSTGRES, official_sources
from scripts.start_database_ci import NAME, main, start, stop


def test_database_falls_back_to_identical_official_image_and_waits_for_health():
    calls, sleeps, states = [], [], iter(["starting", "healthy"])

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            return SimpleNamespace(returncode=int(command[-1] == POSTGRES))
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(stdout=next(states))
        assert kwargs["check"] is True
        return SimpleNamespace(returncode=0)

    start(run=run, sleep=sleeps.append)
    assert calls[:2] == [["docker", "pull", image] for image in official_sources(POSTGRES)]
    launched = next(command for command in calls if command[:2] == ["docker", "run"])
    assert launched[-1] == official_sources(POSTGRES)[1]
    assert "127.0.0.1:5432:5432" in launched and "POSTGRES_PASSWORD=test-only" in launched
    assert launched[launched.index("--name") + 1] == NAME
    assert sleeps == [2]


def test_both_pull_failures_never_start_a_container():
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    with pytest.raises(RuntimeError, match="Both official sources failed"):
        start(run=run)
    assert len(calls) == 2 and all(command[1] == "pull" for command in calls)


@pytest.mark.parametrize("status,attempts", [("unhealthy", 1), ("starting", 30)])
def test_unhealthy_or_stuck_database_fails_before_tests(status, attempts):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=status)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        start(run=run, sleep=lambda _seconds: None)
    assert sum(command[1] == "inspect" for command in calls) == attempts
    assert calls[-1] == ["docker", "logs", NAME]


@pytest.mark.parametrize("exists", [False, True])
def test_cleanup_only_removes_the_exact_disposable_container(exists):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["check"] is True
        return SimpleNamespace(stdout="fictional-container-id" if exists else "")

    stop(run=run)
    assert f"name=^/{NAME}$" in calls[0]
    assert calls[1:] == ([["docker", "rm", "--force", "--volumes", NAME]] if exists else [])


def test_docker_daemon_failure_is_not_mistaken_for_an_absent_container():
    def fail(command, **_kwargs):
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(subprocess.CalledProcessError):
        stop(run=fail)


def test_cli_refuses_non_ci_execution(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="only operates inside GitHub Actions"):
        main([])
