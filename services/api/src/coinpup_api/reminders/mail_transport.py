"""Worker-only SMTP settings and certificate-verified transport."""

import ipaddress
import re
import ssl
from contextlib import suppress
from smtplib import SMTP, SMTP_SSL
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from coinpup_api.reminders.mail_attempt import single_mailbox

_DNS_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")


def _valid_host(host):
    if not host or len(host) > 253:
        return False
    if "%" not in host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return True
    return all(_DNS_LABEL.fullmatch(label) for label in host.split("."))


class SmtpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COINPUP_REMINDER_MAIL_SMTP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    enabled: bool = False
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    mode: Literal["starttls", "ssl"] | None = None
    sender: str | None = None
    username: SecretStr | None = Field(default=None, repr=False)
    password: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def complete_transport(self):
        if not self.enabled:
            return self
        if not _valid_host(self.host) or self.port is None or self.mode is None:
            raise ValueError("Verified SMTP requires a host, port and TLS mode.")
        single_mailbox(self.sender)
        if (self.username is None) != (self.password is None):
            raise ValueError("Supply both SMTP credentials or omit both.")
        for secret in (self.username, self.password):
            if secret is not None:
                value = secret.get_secret_value()
                if not value or any(not 32 <= ord(char) < 127 for char in value):
                    raise ValueError("Invalid SMTP authentication settings.")
        return self


def verified_connection(settings: SmtpSettings):
    """Return a verified connection, optionally authenticated. Never fall back to plaintext."""
    if not settings.enabled:
        raise ValueError("Production reminder SMTP is disabled.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    if settings.mode == "ssl":
        session = SMTP_SSL(settings.host, settings.port, timeout=10, context=context)
    elif settings.mode == "starttls":
        session = SMTP(settings.host, settings.port, timeout=10)
    else:
        raise ValueError("Choose a verified TLS mode.")
    try:
        if settings.mode == "starttls":
            session.starttls(context=context)
        if settings.username is not None and settings.password is not None:
            session.login(
                settings.username.get_secret_value(), settings.password.get_secret_value()
            )
    except BaseException:
        with suppress(OSError):
            session.close()
        raise
    return session
