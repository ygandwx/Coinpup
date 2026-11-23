"""Every saved Invoice fact is compared with its actual immutable current source."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_current import BusinessDocumentCurrentReads
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.invoice_snapshot_reads import read_invoice_render_input
from coinpup_api.ledger.schemas import AssetUpdate
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import canonical, reject, reviewed
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_document_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def replace_document(values, document):
    data = json.loads(values["snapshot_json"])
    data["document"] = document
    content = canonical(data)
    return values | dict(
        snapshot_json=content, snapshot_sha256=hashlib.sha256(content.encode()).hexdigest()
    )


@pytest.mark.parametrize("count", [1, 200])
def test_full_database_projection_matches_actual_original_and_corrected_zero_tail_input(
    company, count
):
    s = company
    values = reviewed(s, count, logo=True)
    before = snapshot(s)
    with s["engine"].connect() as c:
        expected, provided = c.execute(
            text(
                "SELECT coinpup_invoice_document_input(:ledger,:document,:version), "
                "coinpup_invoice_document_timestamps(CAST(:input AS jsonb))"
            ),
            {
                "ledger": s["ledger"],
                "document": values["document_id"],
                "version": values["document_version"],
                "input": canonical(json.loads(values["snapshot_json"])["document"]),
            },
        ).one()
    assert expected == provided
    assert len(expected["current"]["document"]["lines"]) == count
    if count == 200:
        assert expected["current"]["document"]["lines"][-1]["total_amount"] == "0.00"
        assert len(expected["current"]["postings"]) == 199
    assert snapshot(s) == before
    reject(s, values, "ck_invoice_reservation_complete")


@pytest.mark.parametrize(
    "path,value",
    [
        (("asset", "code"), "BTC"),
        (("asset", "scale"), 18),
        (("asset", "network"), "fictional-network"),
        (("asset", "kind"), "crypto"),
        (("asset", "token_reference"), "fictional-token"),
        (("current", "confirmation_id"), "00000000-0000-4000-8000-000000000001"),
        (("current", "revision_id"), "00000000-0000-4000-8000-000000000001"),
        (("current", "postings", 0, "operation_id"), "00000000-0000-4000-8000-000000000001"),
        (("current", "postings", 0, "journal_id"), "00000000-0000-4000-8000-000000000001"),
        (("current", "postings", 0, "operation_version"), 2),
        (("current", "postings", 0, "source_line_id"), "00000000-0000-4000-8000-000000000001"),
        (("current", "document", "id"), "00000000-0000-4000-8000-000000000001"),
        (("current", "document", "version"), 3),
        (("current", "document", "archived"), True),
        (("current", "document", "state"), "draft"),
        (("current", "document", "issuer_snapshot", "name"), "Forged company"),
        (("current", "document", "party_snapshot", "name"), "Forged customer"),
        (("current", "document", "notes"), "Forged note"),
        (("current", "document", "due_date"), "2026-12-31"),
        (("current", "document", "issue_date"), "2026-12-01"),
        (("current", "document", "created_at"), "2026-01-01T00:00:00Z"),
        (("current", "document", "confirmed_at"), "2026-01-01T00:00:00Z"),
        (("current", "document", "line_count"), 2),
        (("current", "document", "net_amount"), "0.02"),
        (("current", "document", "tax_amount"), "0.01"),
        (("current", "document", "total_amount"), "0.02"),
        (("current", "document", "lines", 0, "description"), "Forged line"),
        (("current", "document", "lines", 0, "quantity"), "1.00"),
        (("current", "document", "lines", 0, "unit_price"), "0.010"),
        (("current", "document", "lines", 0, "discount_amount"), "0.00"),
        (("current", "document", "lines", 0, "tax_rate_percent"), "0.0"),
        (("current", "document", "lines", 0, "category_snapshot", "name"), "Forged category"),
        (("current", "document", "lines", 0, "project_snapshot"), {"name": "Forged project"}),
        (("current", "document", "lines", 0, "recognition_date"), "2026-12-01"),
        (("current", "document", "lines", 0, "created_at"), "2026-01-01T00:00:00Z"),
        (("current", "document", "lines", 0, "total_amount"), "0.02"),
        (("current", "document", "extra"), "Fictional"),
    ],
)
def test_rehash_cannot_change_any_original_input_metadata_identity_or_exact_amount(
    company, path, value
):
    s = company
    values = reviewed(s)
    data = json.loads(values["snapshot_json"])["document"]
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    reject(s, replace_document(values, data), "ck_invoice_document_binding")


@pytest.mark.parametrize(
    "fault", ["missing_line", "extra_line", "order", "missing_posting", "extra_posting"]
)
def test_full_ordered_200_line_group_cannot_lose_duplicate_or_reorder_a_member(company, fault):
    s = company
    values = reviewed(s, 200)
    data = json.loads(values["snapshot_json"])["document"]
    rows = data["current"]["document"]["lines"]
    postings = data["current"]["postings"]
    if fault == "missing_line":
        rows.pop()  # Removing the zero-valued tail must still fail.
    elif fault == "extra_line":
        rows.append(rows[-1])
    elif fault == "order":
        rows[0], rows[1] = rows[1], rows[0]
    elif fault == "missing_posting":
        postings.pop()
    else:
        postings.append(postings[-1])
    reject(s, replace_document(values, data), "ck_invoice_document_binding")


@pytest.mark.parametrize("cancel", [False, True])
def test_saved_snapshot_cannot_reserve_after_the_current_document_is_corrected_or_cancelled(
    company, cancel
):
    s = company
    values = reviewed(s)
    current = BusinessDocumentCurrentReads(s["engine"]).current(
        s["owner"], s["ledger"], values["document_id"]
    )
    change = (
        ConfirmedDocumentCancellation(
            expected_version=current.version, reason="Fictional cancellation"
        )
        if cancel
        else correction(current.document)
    )
    send(s, current.document, change, "fictional-after-snapshot")
    reject(s, values, "ck_invoice_document_binding")


def test_equivalent_timezone_spelling_and_disabled_asset_do_not_reprice_saved_facts(company):
    s = company
    values = reviewed(s)
    data = json.loads(values["snapshot_json"])["document"]
    document = data["current"]["document"]
    for record in [document, *document["lines"]]:
        for key in ("created_at", "updated_at", "confirmed_at"):
            if key in record:
                record[key] = (
                    datetime.fromisoformat(record[key])
                    .astimezone(timezone(timedelta(hours=8)))
                    .isoformat()
                )
    asset = next(a for a in s["structure"].list_assets(s["owner"]) if a.asset_id == "USD")
    s["structure"].update_asset(
        s["owner"], "USD", AssetUpdate(expected_version=asset.version, enabled=False)
    )
    changed = replace_document(values, data)
    assert (
        read_invoice_render_input(
            changed["snapshot_json"].encode(), expected_sha256=changed["snapshot_sha256"]
        ).content
        == changed["snapshot_json"].encode()
    )
    reject(s, changed, "ck_invoice_reservation_complete")


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-11-13 23:00:00Z",
        "2026-11-13T23:00:00+00:00",
        "2026-11-13T23:00:00-00:00",
        "2026-11-13T23:00:00.1234560Z",
        "2026-11-13T23:00:00.000000Z",
        "2026-11-13T23:00:60Z",
        "2026-11-13T24:00:00Z",
        "now",
        "infinity",
        True,
    ],
)
def test_postgres_only_time_spellings_cannot_enter_an_unreadable_snapshot(company, stamp):
    with pytest.raises(IntegrityError) as caught, company["engine"].begin() as c:
        c.execute(
            text("SELECT coinpup_invoice_timestamp(CAST(:value AS jsonb))"),
            {"value": json.dumps(stamp)},
        )
    assert caught.value.orig.diag.constraint_name == "ck_invoice_document_binding"


@pytest.mark.parametrize("scope", ["document", "line", "profile"])
def test_an_equivalent_but_noncanonical_timestamp_is_rejected_in_actual_reserved_input(
    company, scope
):
    s = company
    values = reviewed(s)
    data = json.loads(values["snapshot_json"])
    target = data["profile"] if scope == "profile" else data["document"]["current"]["document"]
    if scope == "line":
        target = target["lines"][0]
    target["created_at"] = target["created_at"].replace("T", " ")
    content = canonical(data)
    reject(
        s,
        values
        | dict(snapshot_json=content, snapshot_sha256=hashlib.sha256(content.encode()).hexdigest()),
        "ck_invoice_document_binding",
    )


@pytest.mark.parametrize("field", ["ledger", "document", "version"])
def test_projection_fails_closed_for_other_scope_or_version(company, field):
    s = company
    values = reviewed(s)
    args = dict(
        ledger=s["ledger"], document=values["document_id"], version=values["document_version"]
    )
    args[field] = 1 if field == "version" else uuid4()
    with s["engine"].connect() as c:
        assert (
            c.scalar(
                text("SELECT coinpup_invoice_document_input(:ledger,:document,:version)"), args
            )
            is None
        )


def test_empty_document_binding_roundtrip_restores_every_function(company):
    before = snapshot(company)
    with company["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        invoice_document_migration()["downgrade"]()
        invoice_document_migration()["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(company) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_document_binding_history_cannot_be_protectively_downgraded(company, history_only):
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
                "CREATE TRIGGER fictional_invoice_document_history AFTER INSERT "
                "ON pg_temp.business_invoice_reservations FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_reservations VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["ledger"]},
            )
        else:
            c.execute(insert(InvoiceReservation).values(**values))
        invoice_document_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_document_downgrade"
    assert snapshot(s) == before
