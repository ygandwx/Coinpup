"""Production SMTP never opens a socket in these transport and CLI tests."""

import json
import ssl
from smtplib import SMTPAuthenticationError, SMTPNotSupportedError, SMTPServerDisconnected
from unittest.mock import Mock, call, create_autospec
from uuid import UUID

import pytest
from coinpup_api.config import Settings
from coinpup_api.jobs import __main__ as cli
from coinpup_api.reminders import mail_transport
from coinpup_api.reminders.mail_attempt import MailDisposition, ReminderMail, attempt_mail
from coinpup_api.reminders.mail_transport import SmtpSettings, verified_connection
from pydantic import SecretStr, ValidationError

HOST = "smtp.example.test"
SENDER = "fictional-notifier@example.test"
IDENTITY = "fictional-user"
PASSWORD = "fictional-auth-secret"
OWNER, LEDGER = UUID(int=11), UUID(int=12)
EMAIL = ["reminder-email", "--owner", str(OWNER), "--ledger", str(LEDGER)]
DELIVERY = ["reminder-delivery", "--owner", str(OWNER), "--ledger", str(LEDGER)]


def settings(**changes):
    fields = dict(enabled=True, mode="starttls", host=HOST, port=587, sender=SENDER)
    return SmtpSettings(_env_file=None, **(fields | changes))


@pytest.fixture
def stubs(monkeypatch):
    plain = create_autospec(mail_transport.SMTP)
    implicit = create_autospec(mail_transport.SMTP_SSL)
    monkeypatch.setattr(mail_transport, "SMTP", plain)
    monkeypatch.setattr(mail_transport, "SMTP_SSL", implicit)
    return plain, implicit


@pytest.mark.parametrize(
    ("mode", "authenticated"),
    [("starttls", True), ("starttls", False), ("ssl", True), ("ssl", False)],
)
def test_verified_modes_only_authenticate_after_tls(stubs, mode, authenticated):
    plain, implicit = stubs
    auth = {"username": IDENTITY, "password": PASSWORD} if authenticated else {}
    port = 587 if mode == "starttls" else 465
    session = verified_connection(settings(mode=mode, port=port, **auth))
    if mode == "starttls":
        plain.assert_called_once_with(HOST, port, timeout=10)
        implicit.assert_not_called()
        assert session is plain.return_value
        context = session.starttls.call_args.kwargs["context"]
        expected = [call.starttls(context=context)]
        if authenticated:
            expected.append(call.login(IDENTITY, PASSWORD))
        assert session.method_calls == expected
    else:
        plain.assert_not_called()
        context = implicit.call_args.kwargs["context"]
        implicit.assert_called_once_with(HOST, port, timeout=10, context=context)
        assert session is implicit.return_value
        assert session.method_calls == ([call.login(IDENTITY, PASSWORD)] if authenticated else [])
    assert type(context) is ssl.SSLContext
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "changes",
    [
        {"host": None},
        {"host": "smtp://fictional-user:fictional-auth-secret@example.test"},
        {"port": None},
        {"port": 65536},
        {"mode": None},
        {"mode": "plaintext"},
        {"sender": None},
        {"sender": "bad@example.test\nbcc:another@example.test"},
        {"username": "fictional-user", "password": None},
        {"password": "fictional-auth-secret", "username": None},
        {"password": "", "username": "fictional-user"},
    ],
)
def test_incomplete_or_unverified_transport_is_refused(changes):
    with pytest.raises(ValidationError) as error:
        settings(**changes)
    assert PASSWORD not in str(error.value)


def test_environment_prefix_and_secrets_are_worker_only(monkeypatch):
    for name, value in {
        "ENABLED": "true",
        "HOST": HOST,
        "PORT": "465",
        "MODE": "ssl",
        "SENDER": SENDER,
        "USERNAME": IDENTITY,
        "PASSWORD": PASSWORD,
    }.items():
        monkeypatch.setenv("COINPUP_REMINDER_MAIL_SMTP_" + name, value)
    configured = SmtpSettings(_env_file=None)
    assert configured.enabled and configured.port == 465
    assert isinstance(configured.username, SecretStr) and isinstance(configured.password, SecretStr)
    assert IDENTITY not in repr(configured) and PASSWORD not in configured.model_dump_json()
    assert all("smtp" not in name for name in Settings(_env_file=None).model_dump())


@pytest.mark.parametrize(
    "setup_failure",
    [
        ssl.SSLCertVerificationError("Fictional certificate mismatch"),
        SMTPNotSupportedError("Fictional STARTTLS missing"),
        SMTPAuthenticationError(535, b"Fictional private diagnostic"),
    ],
)
def test_setup_failure_closes_without_sending_or_downgrading(stubs, setup_failure):
    plain, implicit = stubs
    session = plain.return_value
    auth_failed = isinstance(setup_failure, SMTPAuthenticationError)
    target = session.login if auth_failed else session.starttls
    target.side_effect = setup_failure
    config = settings(username=IDENTITY, password=PASSWORD)
    message = ReminderMail(OWNER, SENDER, "fictional-recipient@example.test", "Due", "Fictional")
    result = attempt_mail(message, lambda: verified_connection(config))
    assert result.disposition == MailDisposition.PERMANENT_FAILURE
    if not auth_failed:
        session.login.assert_not_called()
    session.close.assert_called_once_with()
    session.send_message.assert_not_called()
    implicit.assert_not_called()
    assert plain.call_count == 1


def test_disconnected_send_still_has_unknown_outcome(stubs):
    plain, implicit = stubs
    session = plain.return_value
    session.send_message.side_effect = SMTPServerDisconnected("Fictional lost final response")
    config = settings()
    message = ReminderMail(OWNER, SENDER, "fictional-recipient@example.test", "Due", "Fictional")
    assert (
        attempt_mail(message, lambda: verified_connection(config)).disposition
        == MailDisposition.UNKNOWN
    )
    session.send_message.assert_called_once()
    session.close.assert_called_once()
    implicit.assert_not_called()


def test_implicit_tls_failure_cannot_open_a_plain_connection(stubs):
    plain, implicit = stubs
    implicit.side_effect = ssl.SSLCertVerificationError("Fictional rejected chain")
    config = settings(mode="ssl", port=465, username=IDENTITY, password=PASSWORD)
    message = ReminderMail(OWNER, SENDER, "fictional-recipient@example.test", "Due", "Fictional")
    result = attempt_mail(message, lambda: verified_connection(config))
    assert result.disposition == MailDisposition.PERMANENT_FAILURE
    plain.assert_not_called()
    implicit.return_value.login.assert_not_called()
    implicit.return_value.send_message.assert_not_called()


def production():
    return Settings(
        _env_file=None,
        environment="production",
        allowed_origins=["https://fictional.example.test"],
        reminder_mail_capture_enabled=True,
    )


@pytest.mark.parametrize("job", [EMAIL, DELIVERY])
def test_both_cli_entry_points_pass_verified_transport_and_correct_sender(monkeypatch, capsys, job):
    configured = settings(username=IDENTITY, password=PASSWORD)
    monkeypatch.setattr(cli, "Settings", production)
    monkeypatch.setattr(cli, "SmtpSettings", lambda: configured)
    connector = Mock()
    monkeypatch.setattr(cli, "verified_connection", connector)
    monkeypatch.setattr(cli, "capture_connection", Mock(side_effect=AssertionError))
    database = Mock()
    monkeypatch.setattr(cli, "Database", lambda _: database)
    run = Mock(return_value={"inbox": {"delivered": 1}, "email": None})
    target = "run_reminder_email" if job == EMAIL else "run_reminder_delivery"
    monkeypatch.setattr(cli, target, run)
    assert cli.main(job) == 0
    args, options = run.call_args
    assert args[:2] == (database.engine, OWNER)
    assert (args[2] if job == EMAIL else options["ledger"]) == LEDGER
    assert options["sender"] == SENDER and options["connect"]() is connector.return_value
    connector.assert_called_once_with(configured)
    database.close.assert_called_once()
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err and IDENTITY not in output.out + output.err


def test_invalid_production_smtp_blocks_email_but_still_runs_inbox(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", production)
    monkeypatch.setattr(cli, "SmtpSettings", lambda: settings(password=PASSWORD))
    monkeypatch.setattr(cli, "verified_connection", Mock(side_effect=AssertionError))
    database = Mock()
    initialize = Mock(return_value=database)
    monkeypatch.setattr(cli, "Database", initialize)
    inbox = Mock(return_value={"inbox": {"delivered": 1}, "email": None})
    monkeypatch.setattr(cli, "run_reminder_delivery", inbox)
    assert cli.main(EMAIL) == 1
    initialize.assert_not_called()
    assert cli.main(DELIVERY) == 1
    inbox.assert_called_once_with(database.engine, OWNER, ledger=LEDGER, page_size=50)
    output = capsys.readouterr()
    assert json.loads(output.out) == {"email": None, "inbox": {"delivered": 1}}
    assert "SMTP configuration is invalid" in output.err and PASSWORD not in output.err
    database.close.assert_called_once()


def test_nonproduction_never_selects_production_settings(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, environment="test"))
    monkeypatch.setattr(cli, "SmtpSettings", Mock(side_effect=AssertionError))
    monkeypatch.setattr(cli, "Database", Mock(side_effect=AssertionError))
    assert cli.main(EMAIL) == 1
    cli.SmtpSettings.assert_not_called()
    assert "disabled" in capsys.readouterr().err


def test_default_off_production_runs_only_inbox_without_warning(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", production)
    monkeypatch.setattr(cli, "SmtpSettings", lambda: SmtpSettings(_env_file=None))
    monkeypatch.setattr(cli, "verified_connection", Mock(side_effect=AssertionError))
    monkeypatch.setattr(cli, "capture_connection", Mock(side_effect=AssertionError))
    database = Mock()
    monkeypatch.setattr(cli, "Database", lambda _: database)
    inbox = Mock(return_value={"inbox": {"delivered": 1}, "email": None})
    monkeypatch.setattr(cli, "run_reminder_delivery", inbox)
    assert cli.main(DELIVERY) == 0
    inbox.assert_called_once_with(database.engine, OWNER, ledger=LEDGER, page_size=50)
    output = capsys.readouterr()
    assert json.loads(output.out) == {"email": None, "inbox": {"delivered": 1}}
    assert output.err == ""
