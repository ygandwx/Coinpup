"""Transport-only synthetic evidence; no network, user account or external mailbox."""

from email import policy
from email.parser import BytesParser
from smtplib import (
    SMTPAuthenticationError,
    SMTPDataError,
    SMTPNotSupportedError,
    SMTPRecipientsRefused,
    SMTPServerDisconnected,
)
from ssl import SSLCertVerificationError
from uuid import UUID

import pytest
from coinpup_api.reminders.mail_attempt import MailDisposition as D
from coinpup_api.reminders.mail_attempt import (
    MailLeaseEnded,
    MailResult,
    ReminderMail,
    attempt_live_mail,
    attempt_mail,
)


def mail(**changes):
    args = dict(
        delivery_id=UUID(int=7),
        sender="no-reply@example.test",
        recipient="user@example.test",
        subject="虚构提醒 · Deadline",
        body="虚构事项\nFictional item.",
    )
    return ReminderMail(**(args | changes))


class LocalSession:
    def __init__(self, result=None, *, quit_error=False):
        self.result, self.quit_error = result, quit_error
        self.sends, self.closed = [], False

    def send_message(self, msg, *, from_addr, to_addrs):
        self.sends.append((msg.as_bytes(), from_addr, to_addrs))
        if isinstance(self.result, Exception):
            raise self.result
        return {} if self.result is None else self.result

    def quit(self):
        if self.quit_error:
            raise SMTPServerDisconnected("fictional loss after DATA")

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipient", "a@example.test, b@example.test"),
        ("recipient", "a@example.test\r\nBcc: b@example.test"),
        ("sender", "Display <a@example.test>"),
        ("sender", "missing-domain"),
        ("recipient", ""),
        ("recipient", "user@"),
        ("recipient", "<>"),
        ("recipient", 123),
        ("subject", "Heading\nBcc: fictional@example.test"),
        ("subject", "bad\x00header"),
    ],
)
def test_bad_envelope_or_header_never_connects(field, value):
    with pytest.raises(ValueError):
        attempt_mail(mail(**{field: value}), lambda: pytest.fail("Unexpected connection"))


def test_accepted_message_is_plain_utf8_and_quit_failure_never_changes_success():
    session = LocalSession(quit_error=True)
    original = mail()
    assert attempt_mail(original, lambda: session) == MailResult(D.ACCEPTED)
    ((serialized, sender, recipients),) = session.sends
    parsed = BytesParser(policy=policy.default).parsebytes(serialized)
    assert sender == original.sender and recipients == [original.recipient] and session.closed
    assert str(parsed["To"]) == original.recipient and str(parsed["Subject"]) == original.subject
    assert parsed.get_content_type() == "text/plain" and parsed.get_content_charset() == "utf-8"
    assert parsed.get_content().splitlines() == original.body.splitlines()
    assert all(value not in repr(original) for value in (sender, original.subject, original.body))
    assert len(parsed.get_all("Message-ID")) == 1
    assert parsed["Message-ID"] == mail(recipient="changed@example.test").message()["Message-ID"]
    assert parsed["Message-ID"] != mail(delivery_id=UUID(int=8)).message()["Message-ID"]


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError(), MailResult(D.TEMPORARY_FAILURE)),
        (SSLCertVerificationError(), MailResult(D.PERMANENT_FAILURE)),
        (SMTPNotSupportedError("No TLS"), MailResult(D.PERMANENT_FAILURE)),
        (SMTPServerDisconnected("fictional setup"), MailResult(D.TEMPORARY_FAILURE)),
        (SMTPAuthenticationError(535, b"private diagnostic"), MailResult(D.PERMANENT_FAILURE, 535)),
        (SMTPAuthenticationError(454, b"private diagnostic"), MailResult(D.TEMPORARY_FAILURE, 454)),
    ],
)
def test_connect_and_authentication_failures_never_claim_acceptance(exc, expected):
    def connect():
        raise exc

    assert attempt_mail(mail(), connect) == expected


@pytest.mark.parametrize(
    "reply,expected",
    [
        (TimeoutError(), MailResult(D.UNKNOWN)),
        (SMTPRecipientsRefused({}), MailResult(D.UNKNOWN)),
        (SMTPServerDisconnected("possibly accepted"), MailResult(D.UNKNOWN)),
        (SMTPDataError(451, b"private diagnostic"), MailResult(D.TEMPORARY_FAILURE, 451)),
        (SMTPDataError(554, b"private diagnostic"), MailResult(D.PERMANENT_FAILURE, 554)),
        (SMTPDataError(299, b"unusable reply"), MailResult(D.UNKNOWN)),
        (SMTPDataError("454", b"malformed reply"), MailResult(D.UNKNOWN)),
        (SMTPRecipientsRefused({"user@example.test": ()}), MailResult(D.UNKNOWN)),
        ({"user@example.test": ("450", b"malformed reply")}, MailResult(D.UNKNOWN)),
        (False, MailResult(D.UNKNOWN)),
        ([], MailResult(D.UNKNOWN)),
        (
            SMTPRecipientsRefused({"user@example.test": (550, b"private")}),
            MailResult(D.PERMANENT_FAILURE, 550),
        ),
        ({"user@example.test": (450, b"private")}, MailResult(D.TEMPORARY_FAILURE, 450)),
        ({"other@example.test": (450, b"private")}, MailResult(D.UNKNOWN)),
    ],
)
def test_single_send_keeps_ambiguous_outcome_and_explicit_refusal_distinct(reply, expected):
    session = LocalSession(reply, quit_error=True)
    result = attempt_mail(mail(), lambda: session)
    assert len(session.sends) == 1 and session.closed and result == expected
    assert "private" not in repr(result)


@pytest.mark.parametrize(
    "disposition,code",
    [
        (D.ACCEPTED, 550),
        (D.TEMPORARY_FAILURE, 250),
        (D.PERMANENT_FAILURE, 451),
        (D.UNKNOWN, 250),
        (D.UNKNOWN, True),
        (D.TEMPORARY_FAILURE, "fictional private diagnostic"),
        (D.TEMPORARY_FAILURE, 450.0),
        ("accepted", None),
    ],
)
def test_result_rejects_contradictory_private_or_nonnumeric_evidence(disposition, code):
    with pytest.raises(ValueError) as error:
        MailResult(disposition, code)
    assert "fictional private diagnostic" not in str(error.value)


def test_expired_lease_is_blocked_even_before_connecting():
    with pytest.raises(MailLeaseEnded):
        attempt_live_mail(mail(), lambda: pytest.fail("Unexpected connection"), lambda: False)


def test_expired_lease_during_connection_closes_without_sending():
    session = LocalSession()
    checks = iter((True, False))
    with pytest.raises(MailLeaseEnded):
        attempt_live_mail(mail(), lambda: session, lambda: next(checks))
    assert session.sends == [] and session.closed


def test_live_lease_sends_only_once_after_both_checks():
    session = LocalSession()
    checks = iter((True, True))
    assert attempt_live_mail(mail(), lambda: session, lambda: next(checks)) == MailResult(
        D.ACCEPTED
    )
    assert len(session.sends) == 1 and session.closed
