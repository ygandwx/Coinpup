"""The email job scans owned pages and only opens its explicitly enabled local capture."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from coinpup_api.config import Settings
from coinpup_api.jobs import __main__ as cli
from coinpup_api.reminders import mail_job
from coinpup_api.reminders.mail_attempt import MailDisposition, MailResult, ReminderMail
from coinpup_api.reminders.mail_service import EmailClaim

OWNER, LEDGER, FIRST, SECOND = (UUID(int=n) for n in (10, 11, 12, 13))
JOB = ["reminder-email", "--owner", str(OWNER), "--ledger", str(LEDGER)]


@pytest.mark.parametrize("environment,enabled", [("test", False), ("production", True)])
def test_restore_defaults_and_production_never_open_database_or_smtp(
    monkeypatch, capsys, environment, enabled
):
    settings = Settings(
        _env_file=None,
        environment=environment,
        allowed_origins=["https://fictional.example.test"],
        reminder_mail_capture_enabled=enabled,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Database", Mock(side_effect=AssertionError))
    assert cli.main(JOB) == 1
    cli.Database.assert_not_called()
    assert capsys.readouterr().err == "Local reminder email capture is disabled.\n"


def test_capture_settings_default_off_for_an_empty_target():
    assert Settings(_env_file=None).reminder_mail_capture_enabled is False


def test_enabled_job_passes_only_loopback_connector_and_bounded_scope(monkeypatch):
    settings = Settings(
        _env_file=None,
        environment="test",
        reminder_mail_capture_enabled=True,
        reminder_mail_capture_port=10025,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    database = Mock()
    monkeypatch.setattr(cli, "Database", lambda _: database)
    smtp = Mock()
    monkeypatch.setattr(mail_job, "SMTP", smtp)

    def run(engine, owner, ledger, *, sender, connect, page_size):
        assert (engine, owner, ledger) == (database.engine, OWNER, LEDGER)
        assert page_size == 7 and sender == "reminders@coinpup.invalid"
        assert connect() is smtp.return_value
        return {"scanned": 1, "outcomes": {"accepted": 1}}

    monkeypatch.setattr(cli, "run_reminder_email", run)
    assert cli.main([*JOB, "--page-size", "7"]) == 0
    smtp.assert_called_once_with("127.0.0.1", 10025, timeout=10)
    database.close.assert_called_once_with()


def test_runner_scans_each_page_and_sends_only_the_committed_claim(monkeypatch):
    message = ReminderMail(FIRST, "from@example.test", "to@example.test", "Fictional", "Fictional")
    claim = EmailClaim(SECOND, message)
    service = Mock()
    service.candidate_events.side_effect = [[FIRST], [SECOND], []]
    service.claim.side_effect = [claim, None]
    service.is_live.return_value = True
    service.finish.return_value = SimpleNamespace(state="accepted")
    monkeypatch.setattr(mail_job, "ReminderEmailService", lambda _: service)
    smtp = Mock()
    smtp.send_message.return_value = {}
    result = mail_job.run_reminder_email(
        None,
        OWNER,
        LEDGER,
        sender=message.sender,
        connect=lambda: smtp,
        page_size=1,
    )
    assert result == {"scanned": 2, "outcomes": {"accepted": 1}}
    assert [call.kwargs["after"] for call in service.candidate_events.call_args_list] == [
        None,
        FIRST,
        SECOND,
    ]
    assert service.is_live.call_count == 2
    smtp.send_message.assert_called_once()
    service.finish.assert_called_once_with(
        OWNER, LEDGER, claim, MailResult(MailDisposition.ACCEPTED)
    )
