"""Real, durable pending reservations; no fictional success receipt or PDF is implied."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.invoice_issuance_reads import InvoiceIssuanceReads
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.business.invoice_reservation_inputs import FreshInvoiceLogo, InvoiceLogoCheck
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_invoice_command_key import financial
from tests.integration.business.test_invoice_predecessor import advance, next_input
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_reservation_command import args
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_activation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("count", [1, 200])
def test_committed_pending_keeps_original_hash_and_number_and_replays_after_archive(company, count):
    s = company
    row = reviewed(s, count, logo=True, uppercase=True)
    service = InvoiceReservationCommand(s["engine"])
    command = args(s, row)
    before = snapshot(s)
    logo = service.reserve(**command)
    assert isinstance(logo, FreshInvoiceLogo) and logo.identity.file_id == row["logo_file_id"]
    assert snapshot(s) == before
    # This storage fixture supplies synthetic file metadata without private bytes.
    result = service.reserve(**command, logo_check=InvoiceLogoCheck(logo, True))
    assert result.state == "pending" and result.artifacts == ()
    assert (result.id, result.sequence, result.number, result.snapshot_sha256) == (
        row["id"],
        "1",
        row["number"],
        row["snapshot_sha256"],
    )
    after = snapshot(s)
    assert {k for k in before if before[k] != after[k]} == {
        "business_invoice_reservations",
        "change_log",
    }
    (saved,) = after["business_invoice_reservations"]
    assert all(saved[key] == value for key, value in row.items())
    assert len(after["change_log"]) == 1 + len(before["change_log"])
    notice = after["change_log"][-1]
    assert (
        notice["entity_type"],
        notice["entity_id"],
        notice["owner_id"],
        notice["ledger_id"],
        notice["entity_version"],
        notice["change_kind"],
    ) == ("business_invoice_reservations", str(row["id"]), s["owner"], s["ledger"], 1, "upsert")
    with s["engine"].begin() as c:
        c.execute(
            update(InvoiceProfile)
            .where(InvoiceProfile.id == row["configuration_id"])
            .values(archived=True, version=2)
        )
    prior = snapshot(s)
    assert service.reserve(**command) == result
    reads = InvoiceIssuanceReads(s["engine"])
    assert reads.get(s["owner"], s["ledger"], row["id"]) == result
    with pytest.raises(LedgerError) as hidden:
        reads.get(uuid4(), s["ledger"], row["id"])
    assert hidden.value.status == 404
    changed = json.loads(row["request_json"])
    changed["id"] = changed["id"].lower()
    with pytest.raises(LedgerError) as conflict:
        service.reserve(**args(s, row, body=changed))
    assert conflict.value.code == "idempotency_conflict"
    assert snapshot(s) == prior


@pytest.mark.parametrize("same", [True, False])
def test_concurrent_original_or_competing_intent_commits_only_one_number(company, same):
    s = company
    row = reviewed(s)
    original = args(s, row)
    other = (
        original
        if same
        else args(
            s,
            row,
            body=json.loads(row["request_json"]) | {"id": str(uuid4())},
            key="fictional-competing",
        )
    )
    barrier = Barrier(2)
    svc = InvoiceReservationCommand(s["engine"])

    def reserve(command):
        barrier.wait(timeout=15)
        try:
            return svc.reserve(**command)
        except LedgerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [
            future.result(timeout=45)
            for future in [pool.submit(reserve, cmd) for cmd in (original, other)]
        ]
    if same:
        assert a == b and a.state == "pending"
    else:
        assert "invoice_predecessor_conflict" in (a, b)
        assert (b if isinstance(a, str) else a).state == "pending"
    stored = snapshot(s)
    assert len(stored["business_invoice_reservations"]) == 1
    assert stored["business_invoice_reservations"][0]["sequence"] == 1
    assert not stored["business_invoice_completions"]
    assert (
        sum(r["entity_type"] == "business_invoice_reservations" for r in stored["change_log"]) == 1
    )


def test_permanent_key_cannot_be_reused_by_finance_but_remains_ledger_scoped(company):
    s = company
    row = reviewed(s)
    service = InvoiceReservationCommand(s["engine"])
    first = service.reserve(**args(s, row))
    _, other_post = financial(s | {"ledger": s["entity"].ledger.id}, row["idempotency_key"])
    other_receipt = other_post()
    _, post = financial(s, row["idempotency_key"])
    before = snapshot(s)
    with pytest.raises(LedgerError) as taken:
        post()
    assert taken.value.status == 409
    assert other_post() == other_receipt and service.reserve(**args(s, row)) == first
    assert snapshot(s) == before


def test_corrected_version_allocates_a_new_linked_number_and_both_snapshots_stay_fixed(company):
    s = company
    row = reviewed(s, uppercase=True)
    service = InvoiceReservationCommand(s["engine"])
    old = service.reserve(**args(s, row))
    advance(s, row["document_id"])
    new = next_input(s, row, 2)
    successor = service.reserve(**args(s, new))
    assert successor.previous_issuance_id == old.id and successor.sequence == "2"
    assert successor.number != old.number and successor.document_version > old.document_version
    before = snapshot(s)
    assert service.reserve(**args(s, row)) == old
    assert service.reserve(**args(s, new)) == successor
    assert snapshot(s) == before
    for record in (row, new):
        with s["engine"].connect() as c:
            assert (
                c.scalar(
                    select(InvoiceReservation.snapshot_json).where(
                        InvoiceReservation.id == record["id"]
                    )
                )
                == record["snapshot_json"]
            )
    with pytest.raises(IntegrityError) as protected, s["engine"].begin() as c:
        c.execute(delete(InvoiceReservation).where(InvoiceReservation.id == old.id))
    assert protected.value.orig.diag.constraint_name == "ck_invoice_reservation_immutable"


def test_empty_activation_roundtrip_restores_full_catalog_and_staging_trigger(company):
    with company["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        sql = text(
            "SELECT tgdeferrable,tginitdeferred FROM pg_trigger WHERE "
            "tgrelid='public.business_invoice_reservations'::regclass AND "
            "tgname='tr_invoice_reservation_complete'"
        )
        assert c.execute(sql).all() == []
        invoice_activation_migration()["downgrade"]()
        assert c.execute(sql).all() == [(True, True)]
        invoice_activation_migration()["upgrade"]()
        assert c.execute(sql).all() == [] and schema_catalog(c) == catalog


@pytest.mark.parametrize(
    "history", ["pending", "business_invoice_reservations", "business_invoice_completions"]
)
def test_activation_refuses_downgrade_with_committed_history_or_either_notification(
    company, history
):
    s = company
    row = reviewed(s)
    if history == "pending":
        InvoiceReservationCommand(s["engine"]).reserve(**args(s, row))
    else:
        # Use the original notification function on a disposable source. Never remove history.
        with s["engine"].begin() as c:
            c.exec_driver_sql(
                f"CREATE TEMP TABLE {history} (id uuid,ledger_id uuid,version int) ON COMMIT DROP"
            )
            c.exec_driver_sql(
                f"CREATE TRIGGER fictional_history AFTER INSERT ON pg_temp.{history} "
                "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text(f"INSERT INTO pg_temp.{history} VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["ledger"]},
            )
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as protected,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        invoice_activation_migration()["downgrade"]()
    assert protected.value.orig.diag.constraint_name == "ck_invoice_activation_downgrade"
    assert snapshot(s) == before
