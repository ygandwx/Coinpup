"""Restore failures retain useful locations without exposing data or connection details."""

from pathlib import Path


def test_backup_failure_location_does_not_include_exception_or_local_data(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_compose_backup import failure_location

    fictional_secret = "fictional-secret-in-connection-or-parameters"
    try:
        raise RuntimeError(fictional_secret)
    except RuntimeError as error:
        result = failure_location(error)
    assert result.startswith("RuntimeError at test_backup_diagnostics.py:")
    assert "test_backup_failure_location" in result
    assert fictional_secret not in result
    assert str(Path(__file__).resolve().parent) not in result
