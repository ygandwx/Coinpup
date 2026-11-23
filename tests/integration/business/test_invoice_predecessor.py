"""Reissued versions extend prior immutable intent without replacing its number or bytes."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_current import BusinessDocumentCurrentReads
from coinpup_api.business.invoice_issuance_hash import invoice_issuance_hash_v2
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_profiles import InvoiceProfileService
from coinpup_api.business.invoice_render_snapshot import freeze_invoice_render_input
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.ledger.assets import ASSETS
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import canonical, reject, reviewed
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_document_migration, invoice_predecessor_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
_AUTOMATIC = object()


def current(s, identifier):
    return BusinessDocumentCurrentReads(s["engine"]).current(s["owner"], s["ledger"], identifier)


def advance(s, identifier):
    facts = current(s, identifier)
    return send(s, facts.document, correction(facts.document), f"fictional-correct-{facts.version}")


def next_input(s, prior, sequence, *, previous=_AUTOMATIC, document_id=None):
    facts = current(s, document_id or prior["document_id"])
    profile = InvoiceProfileService(s["engine"]).get(
        s["owner"], s["ledger"], prior["configuration_id"]
    )
    command = InvoiceIssuanceCreate(
        id=uuid4(),
        document_id=facts.document_id,
        expected_document_version=facts.version,
        configuration_id=profile.id,
        expected_configuration_version=profile.version,
        previous_issuance_id=prior["id"] if previous is _AUTOMATIC else previous,
    )
    raw = canonical(command.model_dump(mode="json"))
    frozen = freeze_invoice_render_input(
        facts,
        ASSETS["USD"],
        profile,
        ledger_id=s["ledger"],
        command=command,
        sequence=str(sequence),
    )
    return prior | dict(
        id=command.id,
        document_id=facts.document_id,
        document_version=facts.version,
        previous_issuance_id=command.previous_issuance_id,
        sequence=sequence,
        number=frozen.decode()["number"]["number"],
        idempotency_key=f"fictional-successor-{sequence}",
        request_json=raw,
        request_hash=invoice_issuance_hash_v2(s["ledger"], command, raw_body=raw.encode()),
        snapshot_json=frozen.content.decode(),
        snapshot_sha256=frozen.sha256,
    )


@contextmanager
def historical_reservations(s, rows):
    # Each source was frozen from an actual committed version before correction.
    # The real closed 0100 layer admits those past facts inside this rollback only.
    # Never disable the complete gate or commit artificial reservation history.
    with (
        s["engine"].connect() as c,
        c.begin() as transaction,
        Operations.context(MigrationContext.configure(c)),
    ):
        catalog = schema_catalog(c)
        migration = invoice_document_migration()
        migration["downgrade"]()
        for row in rows:
            c.execute(insert(InvoiceReservation).values(**row))
        migration["upgrade"]()
        assert schema_catalog(c) == catalog
        try:
            yield c
        finally:
            transaction.rollback()


def test_first_reviewed_invoice_has_no_predecessor_and_still_cannot_commit(company):
    s = company
    values = reviewed(s)
    reject(s, values, "ck_invoice_reservation_complete")
    reject(s, next_input(s, values, 1), "ck_invoice_predecessor")


@pytest.mark.parametrize("previous", [None, "missing", "self"])
def test_corrected_document_must_refer_to_its_actual_previous_reservation(company, previous):
    s = company
    old = reviewed(s)
    advance(s, old["document_id"])
    new = next_input(s, old, 2, previous=None if previous is None else uuid4())
    if previous == "self":
        # The DTO already rejects self-reference. This direct header mismatch is
        # rejected earlier by snapshot intent binding rather than accepted as a chain.
        new["previous_issuance_id"] = new["id"]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, historical_reservations(s, [old]) as c:
        c.execute(insert(InvoiceReservation).values(**new))
    assert caught.value.orig.diag.constraint_name == (
        "ck_invoice_snapshot_intent" if previous == "self" else "ck_invoice_predecessor"
    )
    assert snapshot(s) == before


def test_new_number_cannot_reissue_the_same_reviewed_document_version(company):
    s = company
    old = reviewed(s)
    new = next_input(s, old, 2)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, historical_reservations(s, [old]) as c:
        c.execute(insert(InvoiceReservation).values(**new))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_predecessor"
    assert snapshot(s) == before


@pytest.mark.parametrize("skip_latest", [False, True])
def test_chain_extends_latest_intent_and_preserves_each_older_number_and_exact_snapshot(
    company, skip_latest
):
    s = company
    first = reviewed(s)
    advance(s, first["document_id"])
    second = next_input(s, first, 2)
    advance(s, first["document_id"])
    third = next_input(s, second, 3, previous=first["id"] if skip_latest else second["id"])
    before = snapshot(s)
    with historical_reservations(s, [first, second]) as c:
        if skip_latest:
            with pytest.raises(IntegrityError) as caught:
                c.execute(insert(InvoiceReservation).values(**third))
            assert caught.value.orig.diag.constraint_name == "ck_invoice_predecessor"
        else:
            c.execute(insert(InvoiceReservation).values(**third))
            assert c.execute(
                select(
                    InvoiceReservation.number,
                    InvoiceReservation.snapshot_json,
                    InvoiceReservation.previous_issuance_id,
                ).order_by(InvoiceReservation.sequence)
            ).all() == [
                (first["number"], first["snapshot_json"], None),
                (second["number"], second["snapshot_json"], first["id"]),
                (third["number"], third["snapshot_json"], second["id"]),
            ]
    assert snapshot(s) == before


def test_document_versions_without_an_issuance_do_not_require_an_invented_previous_number(company):
    s = company
    first = reviewed(s)
    advance(s, first["document_id"])
    advance(s, first["document_id"])
    later = next_input(s, first, 2)
    assert later["document_version"] == first["document_version"] + 2
    before = snapshot(s)
    with historical_reservations(s, [first]) as c:
        c.execute(insert(InvoiceReservation).values(**later))
    assert snapshot(s) == before


@pytest.mark.parametrize("linked", [False, True])
def test_another_invoice_in_the_company_has_its_own_chain_but_uses_the_company_sequence(
    company, linked
):
    s = company
    first = reviewed(s)
    other, _ = confirmed(s, "invoice", 1, key="fictional-second-document")
    new = next_input(s, first, 2, previous=first["id"] if linked else None, document_id=other.id)
    before = snapshot(s)
    with historical_reservations(s, [first]) as c:
        if linked:
            with pytest.raises(IntegrityError) as caught:
                c.execute(insert(InvoiceReservation).values(**new))
            assert caught.value.orig.diag.constraint_name == "ck_invoice_predecessor"
        else:
            c.execute(insert(InvoiceReservation).values(**new))
    assert snapshot(s) == before


def test_empty_predecessor_roundtrip_preserves_whole_catalog(company):
    before = snapshot(company)
    with company["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        invoice_predecessor_migration()["downgrade"]()
        invoice_predecessor_migration()["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(company) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_predecessor_history_blocks_downgrade(company, history_only):
    s = company
    values = reviewed(s)
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        if history_only:
            c.exec_driver_sql(
                "CREATE TEMP TABLE business_invoice_reservations "
                "(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
            )
            c.exec_driver_sql(
                "CREATE TRIGGER fictional_invoice_previous_history AFTER INSERT "
                "ON pg_temp.business_invoice_reservations FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_reservations VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["ledger"]},
            )
        else:
            c.execute(insert(InvoiceReservation).values(**values))
        invoice_predecessor_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_predecessor_downgrade"
    assert snapshot(s) == before
