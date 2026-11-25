"""Configuration inputs are explicit: switches never coerce and unsupported clocks fail early."""

import pytest
from coinpup_api.reminders.delivery_commands import ConfigureDelivery
from pydantic import ValidationError

GOOD = dict(
    expected_event_version=1,
    expected_config_version=0,
    enabled=True,
    lead_days=0,
    timezone="Etc/UTC",
    minute=0,
    locale="en",
    inbox_enabled=True,
    email_enabled=False,
    email_recipient=None,
)


@pytest.mark.parametrize(
    "fields",
    [
        {"expected_event_version": 0},
        {"expected_config_version": -1},
        {"expected_config_version": False},
        {"enabled": 1},
        {"minute": 1440},
        {"lead_days": 3661},
        {"lead_days": "7"},
        {"locale": "auto"},
        {"timezone": "not/a-real-zonename"},
        {"timezone": "localtime"},
        {"timezone": "../UTC"},
        {"inbox_enabled": False},
        {"email_enabled": True},
        {"email_recipient": "two@example.test,three@example.test"},
        {"email_recipient": "first@example.test\nCC: other@example.test"},
        {"email_recipient": ""},
        {"planned_at": "2020-01-01"},
        {"version": 99},
    ],
)
def test_reject_unreviewed_input(fields):
    with pytest.raises(ValidationError):
        ConfigureDelivery.model_validate(GOOD | fields)


def test_single_explicit_address_and_disabled_channels_are_valid():
    assert (
        ConfigureDelivery.model_validate(GOOD | dict(enabled=False, inbox_enabled=False)).enabled
        is False
    )
    assert (
        ConfigureDelivery.model_validate(
            GOOD
            | dict(
                email_enabled=True, email_recipient="one@example.test", minute=1439, lead_days=3660
            )
        ).email_recipient
        == "one@example.test"
    )
