"""Uncommitted number probes; the complete reservation gate remains closed in every test."""

from concurrent.futures import Future
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_reservation_storage import reservation as reservation
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_numbering_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.sync.test_change_order_database import start_call

pytestmark = pytest.mark.integration


def test_numbering_review_reads_the_committed_configuration_after_waiting_for_the_lock(reservation):
    s = reservation
    before = snapshot(s)
    ready, done, backend = Event(), Event(), Future()

    def attempt():
        try:
            with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
                c.exec_driver_sql("SET LOCAL statement_timeout='8000ms'")
                backend.set_result(c.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                c.execute(insert(InvoiceReservation).values(**header(s)))
            return caught.value.orig.diag.constraint_name
        finally:
            done.set()

    with s["engine"].connect() as first:
        transaction = first.begin()
        worker = None
        try:
            first.execute(update(InvoiceProfile).values(version=2, number_prefix="Changed-"))
            worker, result = start_call(attempt, name="fictional-invoice-number-writer")
            assert ready.wait(timeout=5)
            with s["engine"].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 5
                while not observer.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid "
                        "AND wait_event_type='Lock' AND wait_event='advisory')"
                    ),
                    {"pid": backend.result(timeout=1)},
                ):
                    assert monotonic() < deadline
                    assert not done.wait(timeout=0.01)
                assert not done.is_set()
            transaction.commit()
            assert result.result(timeout=5) == "ck_invoice_numbering_configuration"
        finally:
            if transaction.is_active:
                transaction.rollback()
            if worker is not None:
                worker.join(timeout=10)
                assert not worker.is_alive()
    after = snapshot(s)
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"business_invoice_profiles", "change_log"}


def header(s, sequence=1, **changes):
    return (
        s["reservation"]
        | dict(
            id=uuid4(),
            sequence=sequence,
            number="虚构-" + str(sequence).zfill(6),
            document_version=sequence + 1,
            idempotency_key="fictional-number-" + str(sequence),
        )
        | changes
    )


def test_exact_order_and_company_policy_apply_but_commit_is_still_closed(reservation):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        for sequence in range(1, 4):
            c.execute(insert(InvoiceReservation).values(**header(s, sequence)))
        assert c.execute(
            select(InvoiceReservation.sequence, InvoiceReservation.number).order_by(
                InvoiceReservation.sequence
            )
        ).all() == [
            (1, "虚构-000001"),
            (2, "虚构-000002"),
            (3, "虚构-000003"),
        ]
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize("sequence", [0, 2, 5])
def test_new_company_starts_at_one_and_cannot_skip_or_rewind(reservation, sequence):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**header(s, sequence)))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_numbering_sequence"
    assert snapshot(s) == before


@pytest.mark.parametrize("number", ["虚构-1", "虚构-000002", "Other-000001", "", "虚构-0000001"])
def test_number_must_match_exact_reviewed_prefix_and_minimum_digits(reservation, number):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**header(s, number=number)))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_numbering_policy"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["stale", "missing", "archived_profile", "archived_company"])
def test_new_number_requires_the_reviewed_active_company_configuration(reservation, fault):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        changes = {}
        if fault == "stale":
            changes["configuration_version"] = 2
        elif fault == "missing":
            changes["configuration_id"] = uuid4()
        elif fault == "archived_profile":
            c.execute(update(InvoiceProfile).values(version=2, archived=True))
            changes["configuration_version"] = 2
        else:
            entity = select(Ledger.entity_id).where(Ledger.id == s["profile_ledger"])
            c.execute(update(Entity).where(Entity.id.in_(entity)).values(version=2, archived=True))
        c.execute(insert(InvoiceReservation).values(**header(s, **changes)))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_numbering_configuration"
    assert snapshot(s) == before


def test_prefix_and_width_change_continue_the_same_company_sequence(reservation):
    s = reservation
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as transaction:
        c.execute(insert(InvoiceReservation).values(**header(s)))
        c.execute(
            update(InvoiceProfile).values(version=2, number_prefix="Fictional-", minimum_digits=1)
        )
        c.execute(
            insert(InvoiceReservation).values(
                **header(
                    s,
                    2,
                    configuration_version=2,
                    number="Fictional-2",
                )
            )
        )
        assert c.execute(
            select(InvoiceReservation.sequence, InvoiceReservation.number).order_by(
                InvoiceReservation.sequence
            )
        ).all() == [
            (1, "虚构-000001"),
            (2, "Fictional-2"),
        ]
        transaction.rollback()
    assert snapshot(s) == before


def test_same_prefix_and_number_are_independent_between_two_owned_companies(reservation):
    s = reservation
    company = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="company",
            name="Other fictional numbered company",
            country_code="HK",
            company_type="private_limited",
            base_asset_id="USD",
        ),
    )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as transaction:
        profile, document = uuid4(), uuid4()
        c.execute(
            insert(InvoiceProfile).values(
                **(s["profile"] | dict(id=profile, ledger_id=company.ledger.id))
            )
        )
        c.execute(insert(BusinessDocument).values(id=document, ledger_id=company.ledger.id))
        c.execute(insert(InvoiceReservation).values(**header(s)))
        c.execute(
            insert(InvoiceReservation).values(
                **header(
                    s,
                    ledger_id=company.ledger.id,
                    configuration_id=profile,
                    document_id=document,
                )
            )
        )
        assert c.execute(select(InvoiceReservation.sequence, InvoiceReservation.number)).all() == [
            (1, "虚构-000001"),
            (1, "虚构-000001"),
        ]
        transaction.rollback()
    assert snapshot(s) == before


def test_changed_prefix_collision_is_rejected_without_skipping_a_number(reservation):
    s = reservation
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(update(InvoiceProfile).values(version=2, number_prefix="F-1", minimum_digits=1))
        for sequence in range(1, 11):
            c.execute(
                insert(InvoiceReservation).values(
                    **header(
                        s,
                        sequence,
                        configuration_version=2,
                        number="F-1" + str(sequence),
                    )
                )
            )
        c.execute(update(InvoiceProfile).values(version=3, number_prefix="F-"))
        c.execute(
            insert(InvoiceReservation).values(
                **header(
                    s,
                    11,
                    configuration_version=3,
                    number="F-11",
                )
            )
        )
    assert caught.value.orig.diag.constraint_name == "uq_invoice_reservation_number"
    assert snapshot(s) == before


@pytest.mark.parametrize("previous,width", [(9, 1), (999999, 6), (2**63 - 2, 20), (2**63 - 1, 1)])
def test_large_exact_sequence_width_and_exhaustion_using_uncommitted_previous_schema(
    previous, width, reservation
):
    s = reservation
    before = snapshot(s)
    migration = invoice_numbering_migration()
    with (
        s["engine"].connect() as c,
        c.begin() as transaction,
        Operations.context(MigrationContext.configure(c)),
    ):
        # The real closed 0098 layer admits a boundary row only inside this rollback.
        # Its complete gate is never disabled and prevents this artificial seed committing.
        migration["downgrade"]()
        c.execute(insert(InvoiceReservation).values(**header(s, previous, document_version=2)))
        c.execute(update(InvoiceProfile).values(version=2, minimum_digits=width))
        migration["upgrade"]()
        if previous == 2**63 - 1:
            with pytest.raises(IntegrityError) as caught:
                c.execute(
                    insert(InvoiceReservation).values(**header(s, 1, configuration_version=2))
                )
            assert caught.value.orig.diag.constraint_name == "ck_invoice_numbering_exhausted"
        else:
            value = previous + 1
            number = "虚构-" + str(value).zfill(width)
            c.execute(
                insert(InvoiceReservation).values(
                    **header(
                        s,
                        value,
                        document_version=3,
                        configuration_version=2,
                        number=number,
                    )
                )
            )
            assert (
                c.scalar(
                    select(InvoiceReservation.sequence).where(InvoiceReservation.number == number)
                )
                == value
            )
        transaction.rollback()
    assert snapshot(s) == before


def test_empty_numbering_roundtrip_preserves_the_whole_catalog(reservation):
    s = reservation
    before = snapshot(s)
    migration = invoice_numbering_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        migration["downgrade"]()
        migration["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(s) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_numbering_history_cannot_be_downgraded(reservation, history_only):
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
                "CREATE TRIGGER fictional_number_change AFTER INSERT "
                "ON pg_temp.business_invoice_reservations FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_reservations VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["profile_ledger"]},
            )
        else:
            c.execute(insert(InvoiceReservation).values(**header(s)))
        invoice_numbering_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_numbering_downgrade"
    assert snapshot(s) == before
