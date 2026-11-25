"""Existing immutable reminder config history behind actual scoped browser credentials."""

from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfigRevision
from sqlalchemy import select

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.reminders.test_storage import create

pytestmark = pytest.mark.integration


def command(**changes):
    return {
        "expected_event_version": 1,
        "expected_config_version": 0,
        "enabled": True,
        "lead_days": 7,
        "timezone": "Asia/Shanghai",
        "minute": 540,
        "locale": "zh",
        "inbox_enabled": False,
        "email_enabled": True,
        "email_recipient": "fictional@example.test",
    } | changes


def test_config_keeps_original_clock_and_explicit_revisions(ledger_setup, authenticated_client):
    s = ledger_setup
    client, _, _ = authenticated_client
    event = create(s, manual_due_date=date(2020, 1, 31), manual_reason="Fictional")
    url = f"/api/v1/ledgers/{s['ledger']}/reminders/{event}/delivery-config"
    assert client.get(url).json()["config"] is None
    first = client.put(url, json=command())
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json()["config"]["saved_plan"]["scheduled_at"] == "2020-01-24T01:00:00Z"
    stale = client.put(url, json=command())
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    edit = command(expected_config_version=1, email_recipient="replacement@example.test")
    second = client.put(url, json=edit)
    assert second.status_code == 200
    current = second.json()["config"]
    assert current["version"] == 2 and current["saved_plan"] == first.json()["config"]["saved_plan"]
    assert client.get(url).json() == second.json()
    assert client.put(url, json=edit | {"expected_config_version": 2}).json() == second.json()
    with s["engine"].connect() as connection:
        snapshots = connection.scalars(
            select(ReminderDeliveryConfigRevision.snapshot)
            .where(ReminderDeliveryConfigRevision.config_id == current["id"])
            .order_by(ReminderDeliveryConfigRevision.version)
        ).all()
    assert [row["email_recipient"] for row in snapshots] == [
        "fictional@example.test",
        "replacement@example.test",
    ]


def test_config_rejects_cross_scope_and_requires_write_access(ledger_setup, authenticated_client):
    s = ledger_setup
    client, _, _ = authenticated_client
    event = create(s)
    url = f"/api/v1/ledgers/{s['ledger']}/reminders/{event}/delivery-config"
    other = f"/api/v1/ledgers/{uuid4()}/reminders/{event}/delivery-config"
    assert client.get(other).status_code == 404
    assert client.put(other, json=command()).status_code == 404
    assert (
        client.put(url, json=command(), headers={"x-csrf-token": "fictional-invalid"}).status_code
        == 403
    )
    assert client.put(url, json=command(email_recipient="not an address")).status_code == 422
    assert client.get(url).json()["config"] is None
