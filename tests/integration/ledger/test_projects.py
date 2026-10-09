"""Project identities remain owned, versioned and recoverable before financial use."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessProject
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import project_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def seed(s, **values):
    identifier = uuid4()
    with s["engine"].begin() as c:
        c.execute(
            insert(BusinessProject).values(
                dict(id=identifier, ledger_id=s["ledger"], name="Fictional 同名项目") | values
            )
        )
    return identifier


def test_same_names_do_not_merge_project_identities_or_ledgers(ledger_setup):
    s = ledger_setup
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(kind="personal", name="Fictional other projects", base_asset_id="USD"),
    )
    identifiers = [seed(s), seed(s), seed(s, ledger_id=other.ledger.id)]
    records = [
        r
        for r in ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
        if r.entity_type == "business_projects"
    ]
    assert [r.entity_id for r in records] == list(map(str, identifiers))
    assert [r.ledger_id for r in records] == [s["ledger"], s["ledger"], other.ledger.id]
    assert all(r.owner_id == s["owner"] and r.entity_version == 1 for r in records)


def test_project_edits_archive_restore_and_rollback_are_versioned(ledger_setup):
    s = ledger_setup
    identifier = seed(s)
    for version, changes in [
        (2, dict(name="Fictional renamed", notes="Fictional 备注")),
        (3, dict(archived=True)),
        (4, dict(archived=False)),
    ]:
        with s["engine"].begin() as c:
            c.execute(
                update(BusinessProject)
                .where(BusinessProject.id == identifier)
                .values(version=version, **changes)
            )
    records = [
        r
        for r in ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
        if r.entity_type == "business_projects"
    ]
    assert [(r.entity_version, r.change_kind) for r in records] == [
        (1, "upsert"),
        (2, "upsert"),
        (3, "archive"),
        (4, "restore"),
    ]
    before = snapshot(s)
    with pytest.raises(RuntimeError, match="Fictional rollback"):
        with s["engine"].begin() as c:
            c.execute(
                update(BusinessProject)
                .where(BusinessProject.id == identifier)
                .values(version=5, name="Fictional uncommitted")
            )
            raise RuntimeError("Fictional rollback")
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "delete",
        "id",
        "ledger_id",
        "created_at",
        "version",
        "skip",
        "insert_version",
        "insert_archived",
    ],
)
def test_project_identity_and_version_cannot_be_rewritten(ledger_setup, mutation):
    s = ledger_setup
    identifier = seed(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            if mutation == "delete":
                statement = delete(BusinessProject).where(BusinessProject.id == identifier)
            elif mutation.startswith("insert_"):
                statement = insert(BusinessProject).values(
                    dict(id=uuid4(), ledger_id=s["ledger"], name="Fictional invalid")
                    | (dict(version=2) if mutation == "insert_version" else dict(archived=True))
                )
            else:
                changes = {
                    "id": dict(id=uuid4()),
                    "ledger_id": dict(ledger_id=uuid4()),
                    "created_at": dict(created_at=text("CURRENT_TIMESTAMP + interval '1 day'")),
                    "version": dict(version=1),
                    "skip": dict(version=3),
                }[mutation]
                statement = (
                    update(BusinessProject)
                    .where(BusinessProject.id == identifier)
                    .values(dict(version=2) | changes)
                )
            c.execute(statement)
    assert failure.value.orig.diag.constraint_name == "ck_business_reference_identity"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "values, constraint",
    [
        (dict(ledger_id=uuid4()), "fk_business_projects_ledger"),
        (dict(name="   "), "ck_business_projects_name"),
    ],
)
def test_projects_reject_missing_scope_and_empty_name(ledger_setup, values, constraint):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        seed(s, **values)
    assert failure.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize("history", ["empty", "rows", "logs"])
def test_project_migration_guards_history_and_empty_round_trip(ledger_setup, history):
    s = ledger_setup
    if history != "empty":
        seed(s)
        with s["engine"].begin() as c:
            c.exec_driver_sql(
                "TRUNCATE TABLE "
                + ("change_log" if history == "rows" else "business_projects")
                + " RESTRICT"
            )
    before = snapshot(s)

    def run():
        with s["engine"].begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                project_migration()["downgrade"]()
                project_migration()["upgrade"]()

    if history == "empty":
        run()
        assert snapshot(s) == before
        # Restored change trigger/type must still deliver the first subsequent project.
        identifier = seed(s)
        changes = ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
        assert any(
            r.entity_type == "business_projects" and r.entity_id == str(identifier) for r in changes
        )
    else:
        with pytest.raises(IntegrityError) as failure:
            run()
        assert failure.value.orig.diag.constraint_name == "ck_project_downgrade"
        assert snapshot(s) == before
