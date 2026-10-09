"""Draft identity/profile boundaries in real PostgreSQL; all parties are fictional."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessDocument, BusinessParty
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import draft_header_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_journal_dimensions import initial
from tests.integration.ledger.test_party_profiles import seed
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def fields(s):
    return dict(
        document_kind="invoice",
        state="draft",
        party_id=seed(s, name="Fictional 客户", role="both"),
        asset_id="USD",
        issue_date=date(2026, 10, 9),
        due_date=date(2026, 10, 20),
        notes="Fictional draft 备注",
        issuer_snapshot={"name": "Fictional issuer"},
        party_snapshot={"name": "Fictional 客户"},
    )


def write(s, values):
    identifier = uuid4()
    with s["engine"].begin() as c:
        c.execute(insert(BusinessDocument).values(id=identifier, ledger_id=s["ledger"], **values))
    return identifier


def test_draft_edits_versions_snapshots_and_notifications_do_not_post(ledger_setup):
    s = ledger_setup
    values = fields(s)
    before = snapshot(s)
    identifier = write(s, values)
    for version, archived in ((2, True), (3, False)):
        with s["engine"].begin() as c:
            c.execute(
                update(BusinessDocument)
                .where(BusinessDocument.id == identifier)
                .values(
                    version=version,
                    archived=archived,
                    notes="Fictional edited draft",
                )
            )
    with s["engine"].begin() as c:
        c.execute(
            update(BusinessParty)
            .where(BusinessParty.id == values["party_id"])
            .values(
                version=2,
                name="Fictional renamed party",
            )
        )
        row = c.execute(select(BusinessDocument.__table__)).mappings().one()
        assert row["party_snapshot"] == values["party_snapshot"]
        assert row["issuer_snapshot"] == values["issuer_snapshot"]
        changes = c.execute(
            select(ChangeLog.entity_version, ChangeLog.change_kind)
            .where(
                ChangeLog.entity_type == "business_documents",
                ChangeLog.entity_id == str(identifier),
            )
            .order_by(ChangeLog.seq)
        ).all()
        assert changes == [(1, "upsert"), (2, "archive"), (3, "restore")]
    after = snapshot(s)
    for table in ("financial_operations", "journals", "journal_lines", "command_receipts"):
        assert before[table] == after[table]
    with pytest.raises(RuntimeError, match="Fictional rollback"):
        with s["engine"].begin() as c:
            c.execute(
                update(BusinessDocument)
                .where(BusinessDocument.id == identifier)
                .values(
                    version=4,
                    notes="Fictional rollback",
                )
            )
            raise RuntimeError("Fictional rollback")
    assert snapshot(s) == after


@pytest.mark.parametrize(
    "change",
    [
        {"state": None},
        {"state": "paid"},
        {"document_kind": "unknown"},
        {"party_id": None},
        {"asset_id": None},
        {"issue_date": None},
        {"issuer_snapshot": None},
        {"party_snapshot": None},
        {"issuer_snapshot": []},
        {"party_snapshot": "Fictional invalid snapshot"},
    ],
)
def test_incomplete_draft_is_rejected_atomically(ledger_setup, change):
    s = ledger_setup
    values = fields(s) | change
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        write(s, values)
    assert failure.value.orig.diag.constraint_name == "ck_business_documents_profile"
    assert snapshot(s) == before


@pytest.mark.parametrize("foreign", [False, True])
def test_missing_and_foreign_parties_are_rejected(ledger_setup, foreign):
    s = ledger_setup
    values = fields(s)
    identifier = uuid4()
    if foreign:
        entity = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional other draft owner",
                base_asset_id="USD",
            ),
        )
        identifier = seed(s | {"ledger": entity.ledger.id}, name="Fictional other", role="both")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        write(s, values | {"party_id": identifier})
    assert failure.value.orig.diag.constraint_name == "fk_business_documents_party_ledger"
    assert snapshot(s) == before


def test_missing_asset_is_rejected(ledger_setup):
    s = ledger_setup
    values = fields(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        write(s, values | {"asset_id": "FICTIONAL_MISSING"})
    assert failure.value.orig.diag.constraint_name == "fk_business_documents_asset"
    assert snapshot(s) == before


@pytest.mark.parametrize("mutation", ["delete", "id", "ledger_id", "version", "demote", "promote"])
def test_draft_and_legacy_identity_modes_cannot_be_rewritten(ledger_setup, mutation):
    s = ledger_setup
    values = fields(s)
    identifier = write(s, {} if mutation == "promote" else values)
    before = snapshot(s)
    changes = {"version": 2}
    if mutation in ("id", "ledger_id"):
        changes[mutation] = uuid4()
    elif mutation == "version":
        changes["version"] = 3
    elif mutation == "demote":
        changes.update(dict.fromkeys(values))
    elif mutation == "promote":
        changes.update(values)
    statement = (
        delete(BusinessDocument)
        if mutation == "delete"
        else update(BusinessDocument).values(**changes)
    ).where(BusinessDocument.id == identifier)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            c.execute(statement)
    assert failure.value.orig.diag.constraint_name == "ck_business_reference_identity"
    assert snapshot(s) == before


def test_draft_cannot_enter_financial_journals_and_all_writes_roll_back(ledger_setup):
    s = ledger_setup
    identifier = write(s, fields(s))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        initial(s, {"document_id": identifier})
    assert failure.value.orig.diag.constraint_name == "ck_business_draft_not_postable"
    assert snapshot(s) == before


def test_legacy_facts_and_notifications_survive_real_migration_round_trip(ledger_setup):
    s = ledger_setup
    with s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            draft_header_migration()["downgrade"]()
            c.execute(
                text("INSERT INTO business_documents(id,ledger_id) VALUES (:id,:ledger)"),
                {"id": uuid4(), "ledger": s["ledger"]},
            )
            before = dict(c.execute(text("SELECT * FROM business_documents")).mappings().one())
            log = c.execute(select(ChangeLog.__table__)).mappings().all()
            draft_header_migration()["upgrade"]()
            after = dict(c.execute(select(BusinessDocument.__table__)).mappings().one())
            assert {key: after[key] for key in before} == before
            assert all(after[key] is None for key, _ in draft_header_migration()["_FIELDS"])
            assert c.execute(select(ChangeLog.__table__)).mappings().all() == log
    initial(s, {"document_id": before["id"]})  # Legacy references retain their original semantics.


def test_downgrade_refuses_archived_draft_history(ledger_setup):
    s = ledger_setup
    identifier = write(s, fields(s))
    with s["engine"].begin() as c:
        c.execute(
            update(BusinessDocument)
            .where(BusinessDocument.id == identifier)
            .values(
                version=2,
                archived=True,
            )
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                draft_header_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_document_draft_downgrade"
    assert snapshot(s) == before
