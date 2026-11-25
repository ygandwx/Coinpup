"""One ADR 0038 SMTP attempt. No scheduling, credentials, logging or automatic retries."""

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from email import policy
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from enum import StrEnum
from smtplib import SMTP, SMTPNotSupportedError, SMTPRecipientsRefused, SMTPResponseException
from ssl import SSLCertVerificationError
from uuid import UUID


class MailDisposition(StrEnum):
    ACCEPTED = "accepted"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MailResult:
    disposition: MailDisposition
    # Numeric codes can be retained as evidence; server text may contain private information.
    smtp_code: int | None = None

    def __post_init__(self):
        if not isinstance(self.disposition, MailDisposition):
            raise ValueError("Invalid email outcome.")
        if self.smtp_code is None:
            return
        reply_class = {
            MailDisposition.ACCEPTED: 2,
            MailDisposition.TEMPORARY_FAILURE: 4,
            MailDisposition.PERMANENT_FAILURE: 5,
        }.get(self.disposition)
        if type(self.smtp_code) is not int or self.smtp_code // 100 != reply_class:
            raise ValueError("SMTP reply code does not support this outcome.")


def single_mailbox(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise ValueError("Expected a single SMTP mailbox.")
    try:
        address = Address(addr_spec=value)
    except (ValueError, IndexError, HeaderParseError) as exc:
        raise ValueError("Expected a single SMTP mailbox.") from exc
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("Expected a single SMTP mailbox.")
    return value


@dataclass(frozen=True)
class ReminderMail:
    delivery_id: UUID
    sender: str = field(repr=False)
    recipient: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)

    def message(self) -> EmailMessage:
        if not self.subject.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in self.subject):
            raise ValueError("Expected a single-line reminder subject.")
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = single_mailbox(self.sender)
        msg["To"] = single_mailbox(self.recipient)
        msg["Subject"] = self.subject
        msg["Message-ID"] = f"<{self.delivery_id.hex}@coinpup.invalid>"
        msg.set_content(self.body, subtype="plain", charset="utf-8", cte="quoted-printable")
        return msg


def _response(code: object, *, sending: bool) -> MailResult:
    if type(code) is int:
        if 400 <= code <= 499:
            return MailResult(MailDisposition.TEMPORARY_FAILURE, code)
        if 500 <= code <= 599:
            return MailResult(MailDisposition.PERMANENT_FAILURE, code)
    return MailResult(MailDisposition.UNKNOWN if sending else MailDisposition.PERMANENT_FAILURE)


def _refusal(refused: object, recipient: str) -> MailResult:
    if isinstance(refused, dict) and refused.keys() == {recipient}:
        evidence = refused[recipient]
        if isinstance(evidence, tuple) and len(evidence) == 2:
            return _response(evidence[0], sending=True)
    return MailResult(MailDisposition.UNKNOWN)


def attempt_mail(mail: ReminderMail, connect: Callable[[], SMTP]) -> MailResult:
    """The factory returns an authenticated session and owns cleanup if setup fails."""
    msg = mail.message()
    try:
        session = connect()
    except SMTPResponseException as exc:
        return _response(exc.smtp_code, sending=False)
    except (SSLCertVerificationError, SMTPNotSupportedError):
        return MailResult(MailDisposition.PERMANENT_FAILURE)
    except OSError:
        return MailResult(MailDisposition.TEMPORARY_FAILURE)

    try:
        try:
            refused = session.send_message(msg, from_addr=mail.sender, to_addrs=[mail.recipient])
        except SMTPRecipientsRefused as exc:
            return _refusal(exc.recipients, mail.recipient)
        except SMTPResponseException as exc:
            return _response(exc.smtp_code, sending=True)
        except OSError:
            # A lost response may follow acceptance. Nothing here starts a second attempt.
            return MailResult(MailDisposition.UNKNOWN)
        # smtplib returns an empty recipient map after receiving a final acceptance.
        if isinstance(refused, dict) and not refused:
            return MailResult(MailDisposition.ACCEPTED)
        return _refusal(refused, mail.recipient)
    finally:
        # A failure during QUIT cannot undo a positive final DATA response.
        with suppress(OSError):
            session.quit()
        with suppress(OSError):
            session.close()


class MailLeaseEnded(Exception):
    """The committed lease ended before the transport could send the message."""


def attempt_live_mail(
    mail: ReminderMail, connect: Callable[[], SMTP], is_live: Callable[[], bool]
) -> MailResult:
    """Recheck the database lease before and after connection setup, outside business locks."""

    def live_connection():
        if not is_live():
            raise MailLeaseEnded
        smtp = connect()
        # TLS setup and authentication may have taken longer than the original claim.
        try:
            if not is_live():
                raise MailLeaseEnded
        except BaseException:
            with suppress(OSError):
                smtp.close()
            raise
        return smtp

    return attempt_mail(mail, live_connection)
