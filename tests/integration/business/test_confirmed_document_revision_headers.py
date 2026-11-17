"""Fictional confirmed revisions start at the reviewed version and protect every pending header."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Revision
from coinpup_api.business.draft_schemas import BusinessDraftUpdate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_revision_header_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def revision(drafts):
    s = drafts
    draft, intent = prepare(s)
    data = replacement(draft, notes="Fictional reviewed draft before confirmation").model_dump(
        exclude={"reviewed_references"}
    )
    updated = s["service"].update_draft(
        s["owner"],
        s["ledger"],
        draft.id,
        BusinessDraftUpdate(**data, expected_version=draft.version),
    )
    intent = intent.model_copy(update={"expected_version": updated.version})
    receipt = submit(s, updated, intent)
    assert receipt.document_version > 2
    header = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        document_id=draft.id,
        confirmation_id=receipt.id,
        previous_version=receipt.document_version,
        document_version=receipt.document_version + 1,
        action="correct",
        reason="Fictional revised details",
        created_by=s["owner"],
        idempotency_key="fictional-confirm",
        party_id=updated.party_id,
        asset_id=updated.asset_id,
        issue_date=updated.issue_date,
        due_date=updated.due_date,
        notes=updated.notes,
        party_snapshot=updated.party_snapshot,
    )
    return s, header, updated, intent, receipt


def cancelled(header):
    return header | dict(
        action="cancel",
        party_id=None,
        asset_id=None,
        issue_date=None,
        due_date=None,
        notes=None,
        party_snapshot=None,
    )


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_incomplete_revisions_and_their_change_entry_cannot_commit(revision, cancel, explicit):
    s, header, doc, intent, original = revision
    header = cancelled(header) if cancel else header
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        version = c.scalar(
            select(ChangeLog.entity_version).where(
                ChangeLog.entity_type == Revision.__tablename__,
                ChangeLog.entity_id == str(header["id"]),
            )
        )
        assert version == 1
        if explicit:
            c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failed.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert submit(s, doc, intent) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "changes,constraint",
    [
        ({"previous_version": 2, "document_version": 3}, "ck_document_revision_origin_version"),
        ({"document_version": 7}, "ck_document_revision_versions"),
        ({"previous_revision_id": uuid4()}, "fk_document_revision_previous"),
        ({"document_id": uuid4()}, "fk_document_revision_confirmation"),
        ({"confirmation_id": uuid4()}, "fk_document_revision_confirmation"),
        ({"action": "cancel"}, "ck_document_revision_replacement"),
        ({"issue_date": None}, "ck_document_revision_replacement"),
        ({"party_snapshot": []}, "ck_document_revision_replacement"),
        ({"party_id": uuid4()}, "fk_document_revision_party"),
        ({"asset_id": "FAKE"}, "fk_document_revision_asset"),
        ({"reason": "bad\n"}, "ck_document_revision_reason"),
        ({"created_by": uuid4()}, "fk_business_document_revision_owner"),
    ],
)
def test_invalid_replacement_scope_or_version_is_atomic(revision, changes, constraint):
    s, header, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**(header | changes)))
    assert failed.value.orig.diag.constraint_name == constraint and snapshot(s) == before


@pytest.mark.parametrize(
    "operation,constraint",
    [
        ("isolation", "ck_document_revision_isolation"),
        ("update", "ck_document_revision_immutable"),
        ("delete", "ck_document_revision_immutable"),
        ("after-cancel", "ck_document_revision_terminal"),
        ("fork", "uq_document_revision_version"),
        ("row-downgrade", "ck_document_revision_downgrade"),
    ],
)
def test_history_identity_and_terminal_chain(revision, operation, constraint):
    s, header, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c:
        if operation == "isolation":
            c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        c.execute(
            insert(Revision).values(
                **(cancelled(header) if operation == "after-cancel" else header)
            )
        )
        if operation == "update":
            c.execute(update(Revision).values(reason="Fictional mutation"))
        if operation == "delete":
            c.execute(delete(Revision))
        if operation == "fork":
            c.execute(
                insert(Revision).values(
                    **(header | dict(id=uuid4(), idempotency_key="fictional-fork"))
                )
            )
        if operation == "row-downgrade":
            with Operations.context(MigrationContext.configure(c)):
                document_revision_header_migration()["downgrade"]()
        if operation == "after-cancel":
            c.execute(
                insert(Revision).values(
                    **(
                        header
                        | dict(
                            id=uuid4(),
                            idempotency_key="fictional-later",
                            previous_revision_id=header["id"],
                            previous_version=header["document_version"],
                            document_version=header["document_version"] + 1,
                        )
                    )
                )
            )
    assert failed.value.orig.diag.constraint_name == constraint and snapshot(s) == before


def test_notification_history_alone_refuses_downgrade_and_empty_roundtrip_preserves_original(
    revision,
):
    s, header, doc, intent, receipt = revision
    migration = document_revision_header_migration()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c:
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_document_revisions("
            "id uuid, ledger_id uuid, version int) ON COMMIT DROP"
        )
        c.exec_driver_sql(
            "CREATE TRIGGER fictional_header AFTER INSERT ON pg_temp.business_document_revisions "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_document_revisions VALUES (:id,:ledger,1)"),
            dict(id=header["id"], ledger=s["ledger"]),
        )
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert failed.value.orig.diag.constraint_name == "ck_document_revision_downgrade"
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        expected = definitions(c)
        migration["downgrade"]()
        assert c.scalar(text("SELECT to_regclass('public.business_document_revisions')")) is None
        migration["upgrade"]()
        assert definitions(c) == expected
    assert submit(s, doc, intent) == receipt and snapshot(s) == before
