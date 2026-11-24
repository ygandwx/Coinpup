"""Assemble in the same scoped transaction; the database still refuses any committed allocation."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_numbering import MAX_SEQUENCE, InvoiceNumberingPolicy
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_reservation_inputs import _next_number, new_reservation_input
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_document_revision_commands import send
from tests.integration.business.test_document_revision_journals import cancellation
from tests.integration.business.test_invoice_predecessor import (
    advance,
    current,
    historical_reservations,
    next_input,
)
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def build(s, session, values, **overrides):
    # Match the established lock order used by a future reservation command.
    kwargs = dict(
        owner=s["owner"],
        ledger=s["ledger"],
        command=InvoiceIssuanceCreate.model_validate_json(values["request_json"]),
        key=values["idempotency_key"],
        raw_body=values["request_json"].encode(),
    )
    return new_reservation_input(session, **(kwargs | overrides))


@pytest.mark.parametrize("count,logo", [(1, False), (200, True)])
def test_transient_candidate_matches_frozen_independent_input_and_passes_immediate_guards(
    company, count, logo
):
    s = company
    expected = reviewed(s, count, logo=logo, uppercase=True)
    before = snapshot(s)
    service = LedgerService(s["engine"])
    # Inspect the native deferred constraint; do not replace it to manufacture a commit.
    with (
        pytest.raises(IntegrityError) as rejected,
        Session(s["engine"]) as session,
        session.begin(),
    ):
        acquire_write_lock(session)
        service._ledger(session, s["owner"], s["ledger"], write=True)
        raw = json.dumps(json.loads(expected["request_json"]), indent=2).encode()
        row = build(s, session, expected, raw_body=raw)
        assert inspect(row).transient and not session.new
        assert {k: getattr(row, k) for k in expected} == expected
        assert session.scalar(select(InvoiceReservation.id)) is None
        session.add(row)
        session.flush()
        assert session.get(InvoiceReservation, row.id) is row
    assert rejected.value.orig.diag.constraint_name == "ck_invoice_reservation_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault,error",
    [
        ("document-version", "version_conflict"),
        ("profile-version", "version_conflict"),
        ("absent-profile", "not_found"),
        ("archived-profile", "invoice_profile_archived"),
        ("archived-logo", "invoice_logo_unavailable"),
        ("wrong-predecessor", "invoice_predecessor_conflict"),
        ("raw-mismatch", "invalid_request"),
        ("invalid-key", "invalid_idempotency_key"),
    ],
)
def test_invalid_or_stale_current_facts_do_not_create_any_candidate(company, fault, error):
    s = company
    expected = reviewed(s, logo=True)
    before = snapshot(s)
    service = LedgerService(s["engine"])
    with (
        pytest.raises(LedgerError) as rejected,
        service._transaction(s["owner"], write=True, read_committed=True) as session,
    ):
        service._ledger(session, s["owner"], s["ledger"], write=True)
        raw = json.loads(expected["request_json"])
        if fault == "document-version":
            raw["expected_document_version"] += 1
        elif fault == "profile-version":
            raw["expected_configuration_version"] += 1
        elif fault == "absent-profile":
            raw["configuration_id"] = str(uuid4())
        elif fault == "archived-profile":
            raw["expected_configuration_version"] += 1
            session.execute(
                update(InvoiceProfile)
                .where(InvoiceProfile.id == expected["configuration_id"])
                .values(archived=True, version=2)
            )
        elif fault == "archived-logo":
            session.execute(
                update(StoredFile)
                .where(StoredFile.id == expected["logo_file_id"])
                .values(archived=True, version=2)
            )
        elif fault == "wrong-predecessor":
            raw["previous_issuance_id"] = str(uuid4())
        cmd = InvoiceIssuanceCreate.model_validate(raw)
        if fault == "raw-mismatch":
            raw["id"] = str(uuid4())
        build(
            s,
            session,
            expected,
            command=cmd,
            raw_body=json.dumps(raw).encode(),
            key=" " if fault == "invalid-key" else expected["idempotency_key"],
        )
    assert rejected.value.code == error
    assert snapshot(s) == before


def test_same_document_version_cannot_take_a_second_number_in_the_lock_holder_transaction(company):
    s = company
    first = reviewed(s)
    before = snapshot(s)
    service = LedgerService(s["engine"])
    with (
        pytest.raises(LedgerError) as rejected,
        service._transaction(s["owner"], write=True, read_committed=True) as session,
    ):
        service._ledger(session, s["owner"], s["ledger"], write=True)
        session.add(build(s, session, first))
        session.flush()
        raw = json.loads(first["request_json"]) | {
            "id": str(uuid4()),
            "previous_issuance_id": str(first["id"]),
        }
        build(
            s,
            session,
            first,
            command=InvoiceIssuanceCreate.model_validate(raw),
            raw_body=json.dumps(raw).encode(),
            key="fictional-second-key",
        )
    assert rejected.value.code == "invoice_predecessor_conflict"
    assert snapshot(s) == before


def test_corrected_version_links_previous_reservation_with_a_new_number_and_keeps_original(company):
    s = company
    old = reviewed(s)
    advance(s, old["document_id"])
    expected = next_input(s, old, 2)
    before = snapshot(s)
    with (
        historical_reservations(s, [old]) as connection,
        Session(bind=connection, join_transaction_mode="rollback_only") as session,
    ):
        LedgerService(s["engine"])._ledger(session, s["owner"], s["ledger"], write=True)
        row = build(s, session, expected)
        assert {k: getattr(row, k) for k in expected} == expected
        session.add(row)
        session.flush()
        prior = session.get(InvoiceReservation, old["id"])
        assert (prior.sequence, prior.number, prior.snapshot_sha256) == (
            1,
            old["number"],
            old["snapshot_sha256"],
        )
        assert (
            row.previous_issuance_id == prior.id
            and row.sequence == 2
            and row.number != prior.number
        )
    assert snapshot(s) == before


def test_current_cancelled_document_cannot_start_a_new_issuance(company):
    s = company
    original = reviewed(s)
    doc = current(s, original["document_id"]).document
    send(s, doc, cancellation(doc), "fictional-cancel-before-issuance")
    raw = json.loads(original["request_json"]) | {"expected_document_version": doc.version + 1}
    before = snapshot(s)
    service = LedgerService(s["engine"])
    with (
        pytest.raises(LedgerError) as rejected,
        service._transaction(s["owner"], write=True, read_committed=True) as session,
    ):
        service._ledger(session, s["owner"], s["ledger"], write=True)
        build(
            s,
            session,
            original,
            command=InvoiceIssuanceCreate.model_validate(raw),
            raw_body=json.dumps(raw).encode(),
        )
    assert (rejected.value.code, rejected.value.status) == ("invoice_not_issuable", 409)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "last,expected",
    [
        (10, "invoice_number_conflict"),
        (MAX_SEQUENCE, "invoice_sequence_exhausted"),
        (MAX_SEQUENCE - 1, None),
    ],
)
def test_number_selection_never_skips_a_collision_or_wraps_or_rounds_a_bigint(
    company, last, expected
):
    # Controlled historical projection only: do not change the real table or drop guards.
    s = company
    other = uuid4()
    with s["engine"].begin() as c:
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_invoice_reservations "
            "(id uuid, ledger_id uuid, sequence bigint, number text) ON COMMIT DROP"
        )
        c.execute(
            text(
                "INSERT INTO pg_temp.business_invoice_reservations VALUES "
                "(:id,:ledger,1,'F-11'), (:last_id,:ledger,:last,'F-110'), "
                "(:other_id,:other,:max,'F-9223372036854775807')"
            ),
            dict(
                id=uuid4(),
                ledger=s["ledger"],
                last_id=uuid4(),
                last=last,
                other_id=uuid4(),
                other=other,
                max=MAX_SEQUENCE,
            ),
        )
        with Session(bind=c, join_transaction_mode="rollback_only") as session:
            policy = InvoiceNumberingPolicy(prefix="F-", minimum_digits=1)
            if expected:
                with pytest.raises(LedgerError) as caught:
                    _next_number(session, s["ledger"], policy)
                assert (caught.value.status, caught.value.code) == (409, expected)
            else:
                assert _next_number(session, s["ledger"], policy) == (
                    MAX_SEQUENCE,
                    "F-9223372036854775807",
                )
