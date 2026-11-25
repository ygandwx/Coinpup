"""Dual-channel acceptance with fictional dates and an isolated SMTP substitute."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from smtplib import SMTPDataError
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.reminders.delivery_commands import ConfigureDelivery, ReminderDeliveryCommands
from coinpup_api.reminders.delivery_job import run_reminder_delivery
from coinpup_api.reminders.inbox_models import ReminderInboxDelivery as Inbox
from coinpup_api.reminders.inbox_service import ReminderInboxService
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import reminder_inbox_migration
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.reminders.test_email_config import command
from tests.integration.reminders.test_email_delivery import capture
from tests.integration.reminders.test_storage import create, edit

pytestmark = pytest.mark.integration


def configure(s, event, **settings):
    return ReminderDeliveryCommands(s["engine"]).configure(
        s["owner"], s["ledger"], event, ConfigureDelivery(**command(**settings))
    )


def run(s, **options):
    return run_reminder_delivery(s["engine"], s["owner"], page_size=1, **options)


def reminder_api(s):
    return f"/api/v1/ledgers/{s['ledger']}/reminders"


def test_three_event_kinds_deliver_independently_and_keep_original_read_receipt(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, _, _ = authenticated_client
    base = reminder_api(s)
    inbox_url = base + "/inbox"
    events = {}
    for kind in ("tax", "certificate", "annual"):
        event = create(
            s,
            event_kind=kind,
            title=f"Fictional {kind}",
            manual_due_date=date(2020, 1, 31),
            manual_reason="Fictional due date",
        )
        configure(s, event, inbox_enabled=True)
        events[str(event)] = kind

    assert client.get(inbox_url).json() == []
    smtp = capture(SMTPDataError(450, b"temporary fictional refusal"), {}, {})
    first = run(s, connect=lambda: smtp)
    assert first["inbox"] == {"scanned": 3, "delivered": 3}
    assert first["email"]["outcomes"] == {"accepted": 2, "retry_wait": 1}
    second = run(s, connect=lambda: pytest.fail("A repeat cannot send before the retry time"))
    assert second["inbox"]["delivered"] == 0 and second["email"]["outcomes"] == {}

    page = client.get(inbox_url)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    rows = page.json()
    assert {row["event_id"]: row["event_kind"] for row in rows} == events
    assert all(row["planned_at"] == "2020-01-24T01:00:00Z" for row in rows)
    assert all(row["due_date"] == "2020-01-31" and row["date_basis"] == "manual" for row in rows)
    assert {row["version"] for row in rows} == {1}

    target = next(
        row
        for row in rows
        if client.get(base + "/" + row["event_id"] + "/email-deliveries").json()[0]["state"]
        == "accepted"
    )
    path = inbox_url + "/" + target["id"] + "/read"
    refused = client.put(path, headers={"x-csrf-token": "fictional-invalid"})
    assert refused.status_code == 403
    acknowledged = client.put(path)
    read = acknowledged.json()
    assert acknowledged.status_code == 200 and read["read_at"] is not None
    assert read == target | {"read_at": read["read_at"], "version": 2}
    assert client.put(path).content == acknowledged.content
    assert len(client.get(inbox_url, params={"unread_only": True}).json()) == 2
    assert client.get(base + "/" + target["event_id"]).json()["completed"] is False
    assert (
        client.get(base + "/" + target["event_id"] + "/email-deliveries").json()[0]["state"]
        == "accepted"
    )

    changes = ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
    assert [row.entity_version for row in changes if row.entity_id == target["id"]] == [1, 2]


def test_consent_clock_refresh_completion_and_archive_keep_original_inbox_evidence(
    ledger_setup, authenticated_client
):
    s = ledger_setup
    client, _, _ = authenticated_client
    base = reminder_api(s)
    event = create(s, manual_due_date=date(2099, 1, 31), manual_reason="Fictional future")
    configure(s, event, inbox_enabled=True, email_enabled=False, email_recipient=None)

    def email():
        pytest.fail("Inbox-only configuration cannot send email")

    assert run(s, connect=email)["inbox"]["delivered"] == 0
    edit(
        s,
        event,
        "set_manual",
        manual_due_date=date(2020, 1, 31),
        manual_reason="Fictional earlier date",
        last_reason="Fictional earlier date",
    )
    assert run(s, connect=email)["inbox"]["delivered"] == 1
    original = client.get(base + "/inbox").json()[0]
    edit(s, event, "edit", title="Fictional updated text")
    assert run(s, connect=email)["inbox"]["delivered"] == 0
    assert client.get(base + "/inbox").json() == [original]
    edit(
        s,
        event,
        "set_manual",
        manual_due_date=date(2020, 2, 5),
        manual_reason="Fictional new explicit day",
        last_reason="Fictional new explicit day",
    )
    assert run(s, connect=email)["inbox"]["delivered"] == 1
    new, old = client.get(base + "/inbox").json()
    assert new["id"] != old["id"] and old == original
    assert new["title"] == "Fictional updated text" and new["due_date"] == "2020-02-05"
    edit(s, event, "complete", completed=True)
    assert run(s, connect=email)["inbox"]["delivered"] == 0
    edit(s, event, "archive", archived=True)
    assert run(s, ledger=s["ledger"], connect=email)["inbox"]["delivered"] == 0
    assert client.get(base + "/inbox").json() == [new, old]
    assert client.put(base + "/inbox/" + old["id"] + "/read").status_code == 200


def test_unconsented_scope_and_independent_database_witness(ledger_setup, authenticated_client):
    s = ledger_setup
    client, _, _ = authenticated_client
    base = reminder_api(s)
    email_only = create(s, manual_due_date=date(2020, 1, 31), manual_reason="Fictional")
    configure(s, email_only)
    assert run(s)["inbox"]["delivered"] == 0
    assert run(s, connect=lambda: capture({}))["email"]["outcomes"] == {"accepted": 1}
    assert client.get(base + "/inbox").json() == []

    event = create(s, manual_due_date=date(2020, 2, 5), manual_reason="Fictional")
    configure(s, event, inbox_enabled=True, email_enabled=False, email_recipient=None)
    service = ReminderInboxService(s["engine"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: service.deliver(s["owner"], s["ledger"], event), [1, 2]))
    assert sorted(outcomes) == [False, True]
    (notice,) = client.get(base + "/inbox").json()
    assert client.get(f"/api/v1/ledgers/{uuid4()}/reminders/inbox").status_code == 404

    with s["engine"].connect() as connection:
        saved = connection.execute(select(Inbox.__table__)).mappings().one()
    forged = dict(saved) | {"id": uuid4(), "event_id": email_only}
    writes = (
        (insert(Inbox).values(forged), "ck_reminder_inbox_witness"),
        (update(Inbox).values(title="Altered history"), "ck_reminder_inbox_immutable"),
        (
            update(Inbox).values(version=2, read_at=saved["delivered_at"]),
            "ck_reminder_inbox_immutable",
        ),
        (delete(Inbox), "ck_reminder_inbox_immutable"),
    )
    for statement, constraint in writes:
        with pytest.raises(IntegrityError) as error, s["engine"].begin() as connection:
            connection.execute(statement)
        assert error.value.orig.diag.constraint_name == constraint
    assert client.get(base + "/inbox").json() == [notice]

    with (
        pytest.raises(IntegrityError) as blocked,
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        reminder_inbox_migration()["downgrade"]()
    assert blocked.value.orig.diag.constraint_name == "ck_reminder_inbox_downgrade"
