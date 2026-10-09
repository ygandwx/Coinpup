"""The maintenance CLI reports failures without exposing configuration or inputs."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from coinpup_api.business.recurring_job import run_recurring
from coinpup_api.jobs import __main__ as cli


@pytest.mark.parametrize("failures", [0, 1])
def test_cli_uses_aware_clock_bounded_catchup_and_closes_database(monkeypatch, capsys, failures):
    closed, calls = [], []

    class FakeDatabase:
        engine = object()

        def __init__(self, settings):
            pass

        def close(self):
            closed.append(True)

    def run(engine, *, now, per_rule):
        calls.append((engine, now, per_rule))
        return dict(rules=[], confirmed=0, failures=failures)

    monkeypatch.setattr(cli, "Database", FakeDatabase)
    monkeypatch.setattr(cli, "Settings", lambda: object())
    monkeypatch.setattr(cli, "run_recurring", run)
    assert cli.main(["recurring-invoices", "--per-rule", "7"]) == failures
    assert calls[0][0] is FakeDatabase.engine and calls[0][1].utcoffset() == timedelta(0)
    assert calls[0][2] == 7 and closed == [True]
    assert json.loads(capsys.readouterr().out)["failures"] == failures


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["unknown"],
        ["recurring-invoices", "--per-rule", "0"],
        ["recurring-invoices", "--per-rule", "101"],
        ["recurring-invoices", "--per-rule", "1.5"],
    ],
)
def test_invalid_cli_never_initializes_database(monkeypatch, args):
    def forbidden(*args):
        raise AssertionError("Database should not be opened")

    monkeypatch.setattr(cli, "Database", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_configuration_failure_has_no_private_exception_output(monkeypatch, capsys):
    def failure():
        raise ValueError("Fictional sensitive database connection text")

    monkeypatch.setattr(cli, "Settings", failure)
    assert cli.main(["recurring-invoices"]) == 1
    text = capsys.readouterr()
    assert "unavailable" in text.err and "sensitive" not in text.err and text.out == ""


@pytest.mark.parametrize("value", [0, 101, True, "1"])
def test_job_rejects_invalid_batch_limit_before_database_access(value):
    with pytest.raises(ValueError, match="per-rule limit"):
        run_recurring(None, now=datetime(2026, 1, 1, tzinfo=UTC), per_rule=value)
