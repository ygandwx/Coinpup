import warnings
from unittest.mock import Mock

import pytest
from coinpup_api import admin


def test_cli_rejects_password_arguments():
    with pytest.raises(SystemExit) as error:
        admin.main(["create", "--username", "test-admin", "--password", "never-accepted"])
    assert error.value.code == 2


def test_cli_rejects_noninteractive_input(monkeypatch):
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        admin.main(["create", "--username", "test-admin"])
    assert error.value.code == 2


def test_cli_create_uses_hidden_confirmation_and_closes_database(monkeypatch, capsys):
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(admin.getpass, "getpass", lambda _: "fictional-password-123")
    database = Mock()
    monkeypatch.setattr(admin, "Database", lambda _: database)
    create = Mock()
    monkeypatch.setattr(admin, "create_admin", create)
    assert admin.main(["create", "--username", "test-admin"]) == 0
    create.assert_called_once_with(database.engine, "test-admin", "fictional-password-123")
    database.close.assert_called_once()
    captured = capsys.readouterr()
    assert "fictional-password-123" not in captured.out + captured.err


def test_cli_password_mismatch_never_opens_database(monkeypatch):
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)
    answers = iter(["fictional-password-123", "different-password-123"])
    monkeypatch.setattr(admin.getpass, "getpass", lambda _: next(answers))
    database = Mock()
    monkeypatch.setattr(admin, "Database", database)
    assert admin.main(["reset-password"]) == 1
    database.assert_not_called()


def test_cli_refuses_getpass_echo_fallback(monkeypatch):
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: True)

    def unsafe_prompt(_):
        warnings.warn("Password input may be echoed", admin.getpass.GetPassWarning, stacklevel=1)
        raise AssertionError("getpass must stop before reading echoed input")

    monkeypatch.setattr(admin.getpass, "getpass", unsafe_prompt)
    database = Mock()
    monkeypatch.setattr(admin, "Database", database)
    assert admin.main(["reset-password"]) == 1
    database.assert_not_called()
