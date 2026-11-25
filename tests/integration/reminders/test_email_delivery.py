"""Scoped reminder delivery through a real database and isolated synthetic SMTP."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from smtplib import SMTPDataError, SMTPServerDisconnected
from unittest.mock import Mock
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.reminders.delivery_commands import (
    ConfigureDelivery,
    ReminderDeliveryCommands,
)
from coinpup_api.reminders.mail_job import run_reminder_email
from coinpup_api.reminders.mail_models import ReminderEmailDelivery, ReminderEmailEvidence
from coinpup_api.reminders.mail_service import ReminderEmailService
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import reminder_email_migration
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.reminders.test_email_config import command
from tests.integration.reminders.test_storage import create, edit

pytestmark = pytest.mark.integration
SENDER = "reminders@coinpup.invalid"


@pytest.fixture
def due_mail(ledger_setup, authenticated_client):
    s = ledger_setup
    event = create(s, manual_due_date=date(2020, 1, 31), manual_reason="Fictional")
    ReminderDeliveryCommands(s["engine"]).configure(
        s["owner"], s["ledger"], event, ConfigureDelivery(**command())
    )
    client, _, _ = authenticated_client
    return s, event, client, f"/api/v1/ledgers/{s['ledger']}/reminders/{event}/email-deliveries"


def send(s, connect):
    return run_reminder_email(
        s["engine"], s["owner"], s["ledger"], sender=SENDER, connect=connect, page_size=1
    )


def capture(*replies):
    smtp = Mock()
    smtp.send_message.side_effect = replies
    return smtp


def test_acceptance_keeps_the_frozen_envelope_and_does_not_dispatch_again(due_mail):
    s, _, client, route = due_mail
    smtp = capture({})
    assert send(s, lambda: smtp)["outcomes"] == {"accepted": 1}
    first = client.get(route).json()
    assert first[0]["state"] == "accepted" and first[0]["attempt_no"] == 1
    assert send(s, lambda: pytest.fail("Accepted mail must not reopen SMTP"))["outcomes"] == {}
    assert client.get(route).json() == first

    message = smtp.send_message.call_args.args[0]
    assert smtp.send_message.call_args.kwargs["to_addrs"] == ["fictional@example.test"]
    assert message["Message-ID"] == "<" + first[0]["id"].replace("-", "") + "@coinpup.invalid>"
    with s["engine"].connect() as connection:
        states = connection.execute(
            select(
                ReminderEmailEvidence.state,
                ReminderEmailEvidence.envelope,
            ).order_by(ReminderEmailEvidence.version)
        ).all()
    assert states[0][0] == "sending" and "到期日" in states[0][1]["body"]
    assert states[1] == ("accepted", None) and "2020-01-31" in message.get_content()
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as connection:
        connection.execute(update(ReminderEmailEvidence).values(state="unknown"))
    assert error.value.orig.diag.constraint_name == "ck_reminder_email_evidence_immutable"


def test_known_permanent_failure_can_retry_under_same_delivery_identity(due_mail):
    s, _, client, route = due_mail
    smtp = capture(SMTPDataError(550, b"fictional private diagnostic"), {})
    assert send(s, lambda: smtp)["outcomes"] == {"failed": 1}
    (failed,) = client.get(route).json()
    assert failed["smtp_code"] == 550
    assert (
        send(s, lambda: pytest.fail("Permanent failure requires explicit retry"))["outcomes"] == {}
    )
    action = route + "/" + failed["id"] + "/decision"
    accepted = client.post(action, json={"action": "retry", "expected_version": failed["version"]})
    assert accepted.status_code == 200 and accepted.json()["state"] == "retry_wait"
    assert send(s, lambda: smtp)["outcomes"] == {"accepted": 1}
    (row,) = client.get(route).json()
    assert row["id"] == failed["id"] and row["attempt_no"] == 2 and row["state"] == "accepted"
    original, repeated = smtp.send_message.call_args_list
    assert original.args[0]["Message-ID"] == repeated.args[0]["Message-ID"]


def test_temporary_refusal_waits_for_its_bounded_retry_window(due_mail):
    s, _, client, route = due_mail
    smtp = capture(SMTPDataError(451, b"fictional private diagnostic"))
    assert send(s, lambda: smtp)["outcomes"] == {"retry_wait": 1}
    (waiting,) = client.get(route).json()
    assert waiting["smtp_code"] == 451 and waiting["retry_at"] > waiting["recorded_at"]
    assert send(s, lambda: pytest.fail("Retry time is still in the future"))["outcomes"] == {}


def test_lost_response_never_retries_and_can_only_be_resolved(due_mail):
    s, _, client, route = due_mail
    smtp = capture(SMTPServerDisconnected("Fictional lost acknowledgement"))
    assert send(s, lambda: smtp)["outcomes"] == {"unknown": 1}
    (unknown,) = client.get(route).json()
    path = route + "/" + unknown["id"] + "/decision"
    body = {"expected_version": unknown["version"]}
    assert client.post(path, json=body | {"action": "retry"}).status_code == 409
    response = client.post(
        path, json=body | {"action": "verify_received", "reason": "Fictional confirmation"}
    )
    assert response.status_code == 200 and response.json()["state"] == "verified_received"
    assert (
        send(s, lambda: pytest.fail("Unknown or resolved evidence cannot reopen SMTP"))["outcomes"]
        == {}
    )
    smtp.send_message.assert_called_once()


def test_event_completed_during_connection_cancels_before_smtp(due_mail):
    s, event, client, route = due_mail
    smtp = capture({})

    def connect():
        edit(s, event, "complete", completed=True)
        return smtp

    assert send(s, connect)["outcomes"] == {"cancelled": 1}
    assert client.get(route).json()[0]["state"] == "cancelled"
    smtp.send_message.assert_not_called()
    smtp.close.assert_called_once()


def test_recipient_edited_during_connection_reclaims_unsent_schedule(due_mail):
    s, event, client, route = due_mail
    original_transport = capture({})
    updated_transport = capture({})
    config_url = f"/api/v1/ledgers/{s['ledger']}/reminders/{event}/delivery-config"
    replacement = "updated@example.test"

    def connect():
        response = client.put(
            config_url,
            json=command(expected_config_version=1, email_recipient=replacement),
        )
        assert response.status_code == 200
        return original_transport

    first = send(s, connect)
    (waiting,) = client.get(route).json()
    original_transport.send_message.assert_not_called()
    original_transport.close.assert_called_once()
    assert first["outcomes"] == {"retry_wait": 1}
    assert waiting["state"] == "retry_wait" and waiting["attempt_no"] == 1
    assert waiting["retry_at"] == waiting["recorded_at"] and waiting["smtp_code"] is None

    assert send(s, lambda: updated_transport)["outcomes"] == {"accepted": 1}
    (accepted,) = client.get(route).json()
    assert accepted["id"] == waiting["id"] and accepted["planned_at"] == waiting["planned_at"]
    assert accepted["state"] == "accepted" and accepted["attempt_no"] == 2
    updated_transport.send_message.assert_called_once()
    assert updated_transport.send_message.call_args.kwargs["to_addrs"] == [replacement]
    assert send(s, lambda: pytest.fail("Accepted delivery must not reopen SMTP"))["outcomes"] == {}

    with s["engine"].connect() as connection:
        facts = connection.execute(
            select(
                ReminderEmailEvidence.state,
                ReminderEmailEvidence.retries_used,
                ReminderEmailEvidence.envelope["recipient"].as_string(),
            ).order_by(ReminderEmailEvidence.version)
        ).all()
    assert facts == [
        ("sending", 0, "fictional@example.test"),
        ("retry_wait", 0, None),
        ("sending", 0, replacement),
        ("accepted", 0, None),
    ]


@pytest.mark.slow
def test_two_concurrent_workers_cannot_claim_the_same_mail(due_mail):
    s, event, _, _ = due_mail
    svc = ReminderEmailService(s["engine"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(svc.claim, s["owner"], s["ledger"], event, SENDER)
        second = pool.submit(svc.claim, s["owner"], s["ledger"], event, SENDER)
    claims = [first.result(), second.result()]
    assert sum(claim is not None for claim in claims) == 1


def test_no_orphan_delivery_can_commit_and_existing_history_refuses_downgrade(due_mail):
    s, event, _, _ = due_mail
    with pytest.raises(IntegrityError) as orphan, s["engine"].begin() as connection:
        connection.execute(
            insert(ReminderEmailDelivery).values(
                id=uuid4(),
                ledger_id=s["ledger"],
                event_id=event,
                channel="email",
                planned_at=func.clock_timestamp(),
            )
        )
    assert orphan.value.orig.diag.constraint_name == "ck_reminder_email_identity_claim"

    assert send(s, lambda: capture({}))["outcomes"] == {"accepted": 1}
    with pytest.raises(IntegrityError) as blocked:
        with (
            s["engine"].begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            reminder_email_migration()["downgrade"]()
    assert blocked.value.orig.diag.constraint_name == "ck_reminder_email_downgrade"
