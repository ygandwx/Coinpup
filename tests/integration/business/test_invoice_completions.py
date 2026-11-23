"""Metadata sealing only: all reservations remain uncommittable; no PDF bytes are generated."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_issuance_results import InvoiceIssuanceComplete
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_invoice_predecessor import next_input
from tests.integration.business.test_invoice_profile_storage import file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_completion_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def pdf(s, **changes):
    # Deliberately fictional metadata for SQL constraints, not an actual rendered artifact.
    return file(
        s,
        **(
            dict(
                detected_media_type="application/pdf",
                original_filename="fictional.pdf",
                sha256=uuid4().hex * 2,
            )
            | changes
        ),
    )


def completion(s, reservation, *, files=None):
    ids = files or (pdf(s), pdf(s))
    identity = {
        key: reservation[key]
        for key in (
            "id",
            "ledger_id",
            "document_id",
            "document_version",
            "configuration_id",
            "configuration_version",
            "number",
            "snapshot_sha256",
            "template_version",
        )
    }
    with s["engine"].connect() as c:
        artifacts = []
        for language, identifier in zip(("zh", "en"), ids, strict=True):
            row = (
                c.execute(select(StoredFile.__table__).where(StoredFile.id == identifier))
                .mappings()
                .one()
            )
            artifacts.append(
                dict(
                    language=language,
                    file_id=identifier,
                    sha256=row["sha256"],
                    byte_size=row["byte_size"],
                    media_type=row["detected_media_type"],
                )
            )
    body = InvoiceIssuanceComplete(
        **identity,
        kind="invoice_issuance",
        state="complete",
        sequence=str(reservation["sequence"]),
        previous_issuance_id=reservation.get("previous_issuance_id"),
        numbering=json.loads(reservation["snapshot_json"])["profile"]["numbering"],
        artifacts=artifacts,
    ).model_dump(mode="json")
    return dict(
        id=reservation["id"],
        ledger_id=s["ledger"],
        created_by=s["owner"],
        zh_file_id=ids[0],
        en_file_id=ids[1],
        response=body,
        response_status=201,
    )


def rejected(s, reservation, values, constraint):
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**reservation))
        c.execute(insert(InvoiceCompletion).values(**values))
    assert caught.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def test_complete_bilingual_metadata_has_fixed_dto_and_owned_notification_but_cannot_commit(
    company,
):
    s = company
    reservation = reviewed(s)
    values = completion(s, reservation)
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as transaction:
        c.execute(insert(InvoiceReservation).values(**reservation))
        c.execute(insert(InvoiceCompletion).values(**values))
        row = c.execute(select(InvoiceCompletion.__table__)).mappings().one()
        assert row["response"] == values["response"] and row["response_status"] == 201
        assert c.execute(
            select(
                ChangeLog.owner_id,
                ChangeLog.ledger_id,
                ChangeLog.entity_id,
                ChangeLog.entity_version,
                ChangeLog.change_kind,
            ).where(ChangeLog.entity_type == "business_invoice_completions")
        ).one() == (s["owner"], s["ledger"], str(reservation["id"]), 1, "upsert")
        transaction.rollback()
    assert snapshot(s) == before
    rejected(s, reservation, values, "ck_invoice_reservation_complete")


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "00000000-0000-4000-8000-000000000999"),
        ("ledger_id", "00000000-0000-4000-8000-000000000999"),
        ("document_id", "00000000-0000-4000-8000-000000000999"),
        ("configuration_id", "00000000-0000-4000-8000-000000000999"),
        ("document_version", 100),
        ("configuration_version", 100),
        ("previous_issuance_id", "00000000-0000-4000-8000-000000000999"),
        ("number", "FICTIONAL-999"),
        ("sequence", "2"),
        ("snapshot_sha256", "f" * 64),
        ("template_version", "other-v1"),
        ("state", "pending"),
    ],
)
def test_response_cannot_rebind_any_frozen_identity(company, field, value):
    reservation = reviewed(company)
    values = completion(company, reservation)
    values["response"][field] = value
    rejected(company, reservation, values, "ck_invoice_completion_facts")


@pytest.mark.parametrize("mutation", ["missing", "reversed", "duplicate", "hash", "size", "extra"])
def test_exact_two_artifacts_and_all_fixed_response_fields_are_checked(company, mutation):
    reservation = reviewed(company)
    values = completion(company, reservation)
    body = values["response"]
    if mutation == "missing":
        body["artifacts"].pop()
    elif mutation == "reversed":
        body["artifacts"].reverse()
    elif mutation == "duplicate":
        body["artifacts"][1] = deepcopy(body["artifacts"][0])
    elif mutation in ("hash", "size"):
        body["artifacts"][0]["sha256" if mutation == "hash" else "byte_size"] = (
            "b" * 64 if mutation == "hash" else 13
        )
    else:
        body["unexpected"] = "fictional"
    rejected(company, reservation, values, "ck_invoice_completion_facts")


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "duplicate",
        "other-ledger",
        "image",
        "archived",
        "owner",
        "created-before",
        "status",
        "version",
    ],
)
def test_completion_refuses_wrong_file_scope_state_owner_or_fixed_status(company, case):
    s = company
    reservation = reviewed(s)
    values = completion(s, reservation)
    constraint = "ck_invoice_completion_facts"
    if case == "missing":
        values["zh_file_id"] = uuid4()
    elif case == "duplicate":
        values["zh_file_id"] = values["en_file_id"]
    elif case == "other-ledger":
        values["zh_file_id"] = pdf(s, ledger_id=s["entity"].ledger.id)
    elif case == "image":
        values["zh_file_id"] = file(s)
    elif case == "archived":
        with s["engine"].begin() as c:
            c.execute(
                update(StoredFile)
                .where(StoredFile.id == values["zh_file_id"])
                .values(archived=True, version=2)
            )
    elif case == "owner":
        values["created_by"] = uuid4()
    elif case == "created-before":
        values["created_at"] = datetime(2000, 1, 1, tzinfo=UTC)
    else:
        values["response_status" if case == "status" else "version"] = (
            202 if case == "status" else 2
        )
        constraint = (
            "ck_invoice_completion_status" if case == "status" else "ck_invoice_completion_version"
        )
    rejected(s, reservation, values, constraint)


@pytest.mark.parametrize("remove", [False, True])
def test_sealed_response_is_immutable(company, remove):
    s = company
    reservation = reviewed(s)
    values = completion(s, reservation)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**reservation))
        c.execute(insert(InvoiceCompletion).values(**values))
        c.execute(
            delete(InvoiceCompletion) if remove else update(InvoiceCompletion).values(response={})
        )
    assert caught.value.orig.diag.constraint_name == "ck_invoice_completion_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_originals_cannot_be_reused_even_across_language_columns(company, reverse):
    s = company
    first = reviewed(s)
    one = completion(s, first)
    other, _ = confirmed(s, "invoice", 1, key="fictional-second-document")
    second = next_input(s, first, 2, previous=None, document_id=other.id)
    pair = (one["zh_file_id"], pdf(s))
    two = completion(s, second, files=pair[::-1] if reverse else pair)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        for reservation, completed in [(first, one), (second, two)]:
            c.execute(insert(InvoiceReservation).values(**reservation))
            c.execute(insert(InvoiceCompletion).values(**completed))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_completion_file_reuse"
    assert snapshot(s) == before


def test_later_metadata_archive_never_rewrites_the_original_response(company):
    s = company
    reservation = reviewed(s)
    values = completion(s, reservation)
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as transaction:
        c.execute(insert(InvoiceReservation).values(**reservation))
        c.execute(insert(InvoiceCompletion).values(**values))
        c.execute(
            update(StoredFile)
            .where(StoredFile.id == values["zh_file_id"])
            .values(archived=True, title="Fictional renamed", version=2)
        )
        assert c.execute(select(InvoiceCompletion.response)).scalar_one() == values["response"]
        assert (
            c.execute(
                text("SELECT coinpup_invoice_completion_response(:ledger,:id,:zh,:en)"),
                dict(
                    ledger=s["ledger"],
                    id=reservation["id"],
                    zh=values["zh_file_id"],
                    en=values["en_file_id"],
                ),
            ).scalar_one()
            == values["response"]
        )
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("field", ["id", "ledger_id"])
def test_completion_requires_the_exact_existing_reservation_scope(company, field):
    reservation = reviewed(company)
    values = completion(company, reservation)
    values[field] = uuid4()
    rejected(company, reservation, values, "ck_invoice_completion_facts")


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_completion_rejects_transaction_snapshots_that_can_predate_its_write_lock(
    company, isolation
):
    s = company
    values = completion(s, reviewed(s))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].connect() as c:
        c = c.execution_options(isolation_level=isolation)
        with c.begin():
            c.execute(insert(InvoiceCompletion).values(**values))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_completion_isolation"
    assert snapshot(s) == before


def test_empty_completion_migration_roundtrip_preserves_whole_catalog(company):
    before = snapshot(company)
    with company["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        c.exec_driver_sql("CREATE TEMP TABLE business_invoice_completions (id uuid) ON COMMIT DROP")
        c.exec_driver_sql("CREATE TEMP TABLE change_log (entity_type text) ON COMMIT DROP")
        invoice_completion_migration()["downgrade"]()
        assert c.exec_driver_sql(
            "SELECT to_regclass('public.business_invoice_completions') IS NULL"
        ).scalar_one()
        c.exec_driver_sql("DROP TABLE pg_temp.business_invoice_completions, pg_temp.change_log")
        invoice_completion_migration()["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(company) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_completion_rows_or_notifications_block_protective_downgrade(company, history_only):
    s = company
    reservation = reviewed(s)
    values = completion(s, reservation)
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        if history_only:
            c.exec_driver_sql(
                "CREATE TEMP TABLE business_invoice_completions "
                "(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
            )
            c.exec_driver_sql(
                "CREATE TRIGGER fictional_completion_history AFTER INSERT "
                "ON pg_temp.business_invoice_completions FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            notification = dict(id=uuid4(), ledger=s["ledger"])
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_completions VALUES (:id,:ledger,1)"),
                notification,
            )
            c.exec_driver_sql("DELETE FROM pg_temp.business_invoice_completions")
            assert (
                c.exec_driver_sql(
                    "SELECT count(*) FROM public.business_invoice_completions"
                ).scalar_one()
                == 0
            )
            assert c.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.change_log "
                    "WHERE entity_type='business_invoice_completions' "
                    "AND entity_id=:id AND ledger_id=:ledger)"
                ),
                notification | {"id": str(notification["id"])},
            ).scalar_one()
            c.exec_driver_sql("CREATE TEMP TABLE change_log (entity_type text) ON COMMIT DROP")
        else:
            c.execute(insert(InvoiceReservation).values(**reservation))
            c.execute(insert(InvoiceCompletion).values(**values))
        invoice_completion_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_completion_downgrade"
    assert snapshot(s) == before
