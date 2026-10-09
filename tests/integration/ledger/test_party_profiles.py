"""Real PostgreSQL profile compatibility, immutability and transactional history."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessParty
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import party_profile_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
PROFILE = dict(
    name="Fictional 示例客户",
    role="both",
    legal_name="Fictional Example Ltd",
    email="fictional@example.invalid",
    phone="Fictional phone",
    address="Fictional address",
    tax_identifier="FICTIONAL-NOT-A-TAX-ID",
    notes="Fictional bilingual 备注",
)


def seed(s, **profile):
    identifier = uuid4()
    with s["engine"].begin() as c:
        c.execute(insert(BusinessParty).values(id=identifier, ledger_id=s["ledger"], **profile))
    return identifier


def test_legacy_identity_is_not_backfilled_or_rewritten_during_upgrade(ledger_setup):
    s, identifier = ledger_setup, uuid4()
    with s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            party_profile_migration()["downgrade"]()
            c.execute(
                text("INSERT INTO business_parties(id, ledger_id) VALUES (:id, :ledger)"),
                dict(id=identifier, ledger=s["ledger"]),
            )
            before = dict(c.execute(text("SELECT * FROM business_parties")).mappings().one())
            changes = c.execute(select(ChangeLog.__table__)).mappings().all()
            party_profile_migration()["upgrade"]()
            after = dict(c.execute(select(BusinessParty.__table__)).mappings().one())
            assert {key: after[key] for key in before} == before
            assert all(after[field] is None for field in PROFILE)
            assert c.execute(select(ChangeLog.__table__)).mappings().all() == changes


def test_profile_completion_edit_archive_and_rollback_keep_identity(ledger_setup):
    s = ledger_setup
    identifier = seed(s)
    with s["engine"].begin() as c:
        c.execute(
            update(BusinessParty).where(BusinessParty.id == identifier).values(version=2, **PROFILE)
        )
        c.execute(
            update(BusinessParty)
            .where(BusinessParty.id == identifier)
            .values(version=3, name="Fictional renamed", role="supplier", archived=True)
        )
        c.execute(
            update(BusinessParty)
            .where(BusinessParty.id == identifier)
            .values(version=4, archived=False)
        )
        row = c.execute(select(BusinessParty.__table__)).mappings().one()
        assert row["name"] == "Fictional renamed" and row["role"] == "supplier"
        assert row["email"] == PROFILE["email"] and row["version"] == 4
        changes = c.execute(
            select(ChangeLog.entity_version, ChangeLog.change_kind)
            .where(ChangeLog.entity_id == str(identifier))
            .order_by(ChangeLog.seq)
        ).all()
        assert changes == [(1, "upsert"), (2, "upsert"), (3, "archive"), (4, "restore")]
    before = snapshot(s)
    with pytest.raises(RuntimeError, match="Fictional rollback"):
        with s["engine"].begin() as c:
            c.execute(
                update(BusinessParty)
                .where(BusinessParty.id == identifier)
                .values(version=5, notes="Fictional uncommitted")
            )
            raise RuntimeError("Fictional rollback")
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "profile",
    [
        dict(name="Fictional missing role"),
        dict(role="customer"),
        dict(email="f@example.invalid"),
        dict(name="", role="customer"),
        dict(name="Fictional bad role", role="invalid"),
    ],
)
def test_incomplete_profiles_are_rejected_atomically(ledger_setup, profile):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        seed(s, **profile)
    assert failure.value.orig.diag.constraint_name == "ck_business_parties_profile"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {field: None for field in PROFILE},
        dict(created_at=text("CURRENT_TIMESTAMP + interval '1 day'")),
        dict(id=uuid4()),
        dict(ledger_id=uuid4()),
        dict(version=1),
    ],
)
def test_complete_profile_cannot_be_erased_or_reidentified(ledger_setup, changes):
    s = ledger_setup
    identifier = seed(s, **PROFILE)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            c.execute(
                update(BusinessParty)
                .where(BusinessParty.id == identifier)
                .values(dict(version=2) | changes)
            )
    assert failure.value.orig.diag.constraint_name == "ck_business_reference_identity"
    assert snapshot(s) == before


@pytest.mark.parametrize("history", ["empty", "rows", "logs"])
def test_profile_downgrade_preserves_data_and_logs(ledger_setup, history):
    s = ledger_setup
    if history != "empty":
        seed(s, **PROFILE)
        with s["engine"].begin() as c:
            if history == "rows":
                c.exec_driver_sql("TRUNCATE TABLE change_log RESTRICT")
            else:
                c.exec_driver_sql(
                    "TRUNCATE TABLE recurring_invoice_instances, recurring_invoice_rules, "
                    "journal_lines, business_document_lines, "
                    "business_documents, business_parties RESTRICT"
                )
    before = snapshot(s)

    def round_trip():
        with s["engine"].begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                party_profile_migration()["downgrade"]()
                party_profile_migration()["upgrade"]()

    if history == "empty":
        round_trip()
    else:
        with pytest.raises(IntegrityError) as failure:
            round_trip()
        assert failure.value.orig.diag.constraint_name == "ck_party_profile_downgrade"
    assert snapshot(s) == before
