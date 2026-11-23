"""Closed reservation headers preserve intent, company numbering and immutable evidence."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_invoice_profile_storage import add, file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_reservation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def reservation(setup):
    s = setup
    add(s)
    document_id = uuid4()
    with s["engine"].begin() as c:
        # A legacy scoped identity suffices to test the still-closed storage boundary.
        # No test pretends it is a confirmed, reviewed or issued Invoice.
        c.execute(insert(BusinessDocument).values(id=document_id, ledger_id=s["profile_ledger"]))
    s["reservation"] = dict(
        id=uuid4(),
        ledger_id=s["profile_ledger"],
        document_id=document_id,
        document_version=2,
        configuration_id=s["profile"]["id"],
        configuration_version=1,
        sequence=1,
        number="Fictional-0001",
        template_version="commercial-invoice-v1",
        snapshot_json="{}",
        snapshot_sha256="a" * 64,
        request_json="{}",
        request_hash="b" * 64,
        hash_version=2,
        idempotency_key="fictional-reservation-1",
        created_by=s["owner"],
    )
    return s


def test_closed_commit_rolls_back_reservation_and_transactional_notification(reservation):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**s["reservation"]))
        notice = (
            c.execute(
                select(ChangeLog).where(ChangeLog.entity_type == "business_invoice_reservations")
            )
            .mappings()
            .one()
        )
        assert (notice["owner_id"], notice["ledger_id"], notice["entity_version"]) == (
            s["owner"],
            s["profile_ledger"],
            1,
        )
        assert notice["entity_id"] == str(s["reservation"]["id"])
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("sequence", 0),
        ("sequence", -1),
        ("document_version", 1),
        ("configuration_version", 0),
        ("hash_version", 1),
        ("request_hash", "A" * 64),
        ("snapshot_sha256", "x"),
        ("number", ""),
        ("number", "bad\n"),
        ("template_version", "../unreviewed"),
        ("idempotency_key", "space key"),
        ("request_json", " "),
        pytest.param("request_json", "虚" * 21846, id="request-exceeds-byte-limit"),
        pytest.param("snapshot_json", "虚" * 1419947, id="snapshot-exceeds-byte-limit"),
    ],
)
def test_invalid_header_values_never_reach_the_complete_reservation_gate(reservation, field, value):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**(s["reservation"] | {field: value})))
    assert caught.value.orig.diag.constraint_name.startswith("ck_invoice_reservation_")
    assert caught.value.orig.diag.constraint_name != "ck_invoice_reservation_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize("field", ["idempotency_key", "sequence", "number", "document_version"])
def test_company_intent_number_and_document_version_uniqueness(reservation, field):
    s = reservation
    before = snapshot(s)
    original = s["reservation"]
    second = original | dict(
        id=uuid4(),
        sequence=2,
        number="Fictional-0002",
        document_version=3,
        idempotency_key="fictional-reservation-2",
    )
    second[field] = original[field]
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**original))
        c.execute(insert(InvoiceReservation).values(**second))
    assert caught.value.orig.diag.constraint_name == "uq_invoice_reservation_" + {
        "idempotency_key": "key",
        "document_version": "version",
    }.get(field, field)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "action", ["number", "snapshot_json", "request_hash", "created_at", "delete"]
)
def test_reservation_evidence_cannot_be_edited_or_deleted(reservation, action):
    s = reservation
    before = snapshot(s)
    values = dict(
        number="Fictional-other",
        snapshot_json='{"changed":true}',
        request_hash="c" * 64,
        created_at=text("now() + interval '1 day'"),
    )
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**s["reservation"]))
        statement = (
            delete(InvoiceReservation)
            if action == "delete"
            else update(InvoiceReservation).values(**{action: values[action]})
        )
        c.execute(statement)
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize("scope", ["owner", "document", "configuration", "logo", "personal"])
def test_cross_ledger_or_owner_references_and_personal_company_are_rejected(reservation, scope):
    s = reservation
    if scope == "document":
        identifier = uuid4()
        with s["engine"].begin() as c:
            c.execute(insert(BusinessDocument).values(id=identifier, ledger_id=s["ledger"]))
        changes = {"document_id": identifier}
    elif scope == "configuration":
        company = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="company",
                name="Other fictional company",
                country_code="HK",
                company_type="private_limited",
                base_asset_id="USD",
            ),
        )
        identifier = uuid4()
        with s["engine"].begin() as c:
            c.execute(
                insert(InvoiceProfile).values(
                    **(
                        s["profile"]
                        | dict(
                            id=identifier,
                            ledger_id=company.ledger.id,
                        )
                    )
                )
            )
        changes = {"configuration_id": identifier}
    elif scope == "logo":
        changes = {"logo_file_id": file(s, ledger_id=s["ledger"])}
    else:
        changes = {"created_by": uuid4()} if scope == "owner" else {"ledger_id": s["ledger"]}
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**(s["reservation"] | changes)))
    assert caught.value.orig.diag.constraint_name == (
        "ck_invoice_reservation_company"
        if scope == "personal"
        else "fk_invoice_reservation_" + scope
    )
    assert snapshot(s) == before


def test_predecessor_requires_same_document_and_cannot_point_to_itself(reservation):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(
            insert(InvoiceReservation).values(
                **(s["reservation"] | {"previous_issuance_id": s["reservation"]["id"]})
            )
        )
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_previous"
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        other = uuid4()
        c.execute(insert(BusinessDocument).values(id=other, ledger_id=s["profile_ledger"]))
        c.execute(insert(InvoiceReservation).values(**s["reservation"]))
        c.execute(
            insert(InvoiceReservation).values(
                **(
                    s["reservation"]
                    | dict(
                        id=uuid4(),
                        document_id=other,
                        sequence=2,
                        number="Fictional-2",
                        idempotency_key="fictional-other",
                        previous_issuance_id=s["reservation"]["id"],
                    )
                )
            )
        )
    assert caught.value.orig.diag.constraint_name == "fk_invoice_reservation_previous"
    assert snapshot(s) == before


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_reservation_requires_the_existing_write_transaction_isolation(reservation, isolation):
    s = reservation
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].connect().execution_options(isolation_level=isolation) as c,
        c.begin(),
    ):
        c.execute(insert(InvoiceReservation).values(**s["reservation"]))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_isolation"
    assert snapshot(s) == before


def test_empty_downgrade_upgrade_restores_whole_catalog_and_preserves_other_tables(reservation):
    s = reservation
    before = snapshot(s)
    migration = invoice_reservation_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        migration["downgrade"]()
        assert c.scalar(text("SELECT to_regclass('business_invoice_reservations')")) is None
        migration["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(s) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_existing_reservation_or_notification_blocks_protective_downgrade(
    reservation, history_only
):
    s = reservation
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
                "CREATE TRIGGER fictional_reservation_change AFTER INSERT "
                "ON pg_temp.business_invoice_reservations FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_reservations VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["profile_ledger"]},
            )
        else:
            c.execute(insert(InvoiceReservation).values(**s["reservation"]))
        invoice_reservation_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_downgrade"
    assert snapshot(s) == before
