"""Exercise effective predecessors and actual journal evidence behind the sealed document gate."""

from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.ledger.models import FinancialOperation, Journal
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_accrual_member_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def accrual(header, fact, prior, action):
    return dict(
        id=uuid4(),
        revision_id=header["id"],
        confirmation_id=header["confirmation_id"],
        document_id=header["document_id"],
        ledger_id=header["ledger_id"],
        document_version=header["document_version"],
        source_line_id=fact["source_line_id"],
        replacement_fact_id=fact["id"] if action != "cancel" else None,
        action=action,
        operation_id=uuid4() if action == "create" else prior.operation_id,
        operation_version=1 if action == "create" else 2,
        previous_journal_id=None if action == "create" else prior.journal_id,
        reversal_journal_id=None if action == "create" else uuid4(),
        journal_id=None if action == "cancel" else uuid4(),
    )


def post(c, header, fact, row, fault=None):
    common = dict(
        ledger_id=row["ledger_id"],
        operation_id=row["operation_id"],
        operation_version=row["operation_version"],
    )
    if row["action"] == "create":
        c.execute(
            insert(FinancialOperation),
            dict(
                id=row["operation_id"],
                ledger_id=row["ledger_id"],
                created_by=header["created_by"],
                kind="income" if fault == "operation-kind" else "invoice_accrual",
                current_journal_id=row["journal_id"],
                version=1,
            ),
        )
    if row["previous_journal_id"]:
        old = c.execute(
            select(Journal.__table__).where(Journal.id == row["previous_journal_id"])
        ).one()
        reversal = dict(
            id=row["reversal_journal_id"],
            journal_kind="reversal",
            reverses_journal_id=old.id,
            transaction_date=old.transaction_date,
            recognition_date=old.recognition_date,
            description=old.description,
            revision_reason=header["reason"],
        )
        if fault == "reversal-date":
            reversal["recognition_date"] += timedelta(days=1)
        if fault == "reversal-description":
            reversal["description"] = "Fictional wrong source"
        c.execute(insert(Journal), common | reversal)
    if row["journal_id"]:
        normal = dict(
            id=row["journal_id"],
            journal_kind="posting",
            transaction_date=header["issue_date"],
            recognition_date=fact["recognition_date"],
            description=fact["description"],
            revision_reason=None if row["operation_version"] == 1 else header["reason"],
        )
        if fault == "replacement-date":
            normal["transaction_date"] += timedelta(days=1)
        if fault == "replacement-reason":
            normal["revision_reason"] = "Fictional wrong reason"
        c.execute(insert(Journal), common | normal)


@pytest.mark.parametrize(
    "action,fault",
    [
        ("create", None),
        ("correct", None),
        ("cancel", None),
        ("create", "operation-kind"),
        ("correct", "reversal-date"),
        ("cancel", "reversal-description"),
        ("correct", "replacement-date"),
        ("correct", "replacement-reason"),
    ],
)
def test_scoped_accrual_journal_evidence_and_complete_group_gate(revision, action, fault):
    s, header, doc, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        if action == "cancel":
            header = cancelled(header)
        c.execute(insert(Header), header)
        fact = line(header, doc)
        if action == "create":
            fact["source_line_id"] = uuid4()
        if action != "cancel":
            c.execute(insert(Fact), fact)
        link = accrual(header, fact, original.postings[0], action)
        post(c, header, fact, link, fault)
        c.execute(insert(Link), link)
        c.exec_driver_sql("SET CONSTRAINTS tr_document_accrual_journals IMMEDIATE")
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert err.value.orig.diag.constraint_name == (
        "ck_document_accrual_journals" if fault else "ck_document_revision_complete"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault,constraint",
    [
        ("no-positive-fact", "ck_document_accrual_replacement_source"),
        ("other-source", "ck_document_accrual_replacement_source"),
        ("wrong-parent", "fk_document_accrual_group"),
        ("wrong-version", "ck_document_accrual_previous_source"),
        ("wrong-old-journal", "ck_document_accrual_previous_source"),
        ("replaced-operation", "ck_document_accrual_previous_source"),
        ("create-over-old", "ck_document_accrual_previous_source"),
        ("create-reuses-original-operation", "ck_document_accrual_previous_source"),
        ("no-reversal", "ck_document_accrual_previous"),
        ("no-replacement", "ck_document_accrual_next"),
        ("edit", "ck_document_accrual_immutable"),
        ("delete", "ck_document_accrual_immutable"),
        ("duplicate-source", "uq_document_accrual_source"),
        ("downgrade", "ck_document_accrual_downgrade"),
    ],
)
def test_invalid_native_members_abort_with_atomic_feed(revision, fault, constraint):
    s, header, doc, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        fact = line(header, doc)
        if fault == "no-positive-fact":
            fact.update(unit_price="0", tax_amount="0", net_amount="0", total_amount="0")
        if fault == "create-reuses-original-operation":
            fact["source_line_id"] = uuid4()
        c.execute(insert(Fact), fact)
        row = accrual(
            header,
            fact,
            original.postings[0],
            "create" if fault.startswith("create-") else "correct",
        )
        changes = {
            "other-source": dict(source_line_id=uuid4()),
            "wrong-parent": dict(confirmation_id=uuid4()),
            "wrong-version": dict(operation_version=3),
            "wrong-old-journal": dict(previous_journal_id=uuid4()),
            "replaced-operation": dict(operation_id=uuid4()),
            "no-reversal": dict(reversal_journal_id=None),
            "no-replacement": dict(replacement_fact_id=None),
            "create-reuses-original-operation": dict(
                operation_id=original.postings[0].operation_id
            ),
        }
        c.execute(insert(Link), row | changes.get(fault, {}))
        if fault == "edit":
            c.execute(update(Link).values(operation_version=3))
        elif fault == "delete":
            c.execute(delete(Link))
        elif fault == "duplicate-source":
            c.execute(insert(Link), row | {"id": uuid4()})
        elif fault == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                document_accrual_member_migration()["downgrade"]()
    assert err.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize("use_fixed_original", [False, True])
def test_second_revision_must_use_the_immediate_predecessor(revision, use_fixed_original):
    s, header, doc, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        fact = line(header, doc)
        c.execute(insert(Fact), fact)
        first = accrual(header, fact, original.postings[0], "correct")
        post(c, header, fact, first)
        c.execute(insert(Link), first)
        second = cancelled(header) | dict(
            id=uuid4(),
            previous_revision_id=header["id"],
            idempotency_key="fictional-second-accrual",
            previous_version=header["document_version"],
            document_version=header["document_version"] + 1,
        )
        c.execute(insert(Header), second)
        row = accrual(second, fact, original.postings[0], "cancel") | dict(
            operation_version=3,
            previous_journal_id=first["previous_journal_id"]
            if use_fixed_original
            else first["journal_id"],
        )
        c.execute(insert(Link), row)
        post(c, second, fact, row)
        c.exec_driver_sql("SET CONSTRAINTS tr_document_accrual_journals IMMEDIATE")
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert err.value.orig.diag.constraint_name == (
        "ck_document_accrual_previous_source"
        if use_fixed_original
        else "ck_document_revision_complete"
    )
    assert snapshot(s) == before


def test_accrual_feed_history_prevents_downgrade_even_without_business_rows(revision):
    s, header, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_document_accruals(id uuid,ledger_id uuid,version int) "
            "ON COMMIT DROP"
        )
        c.exec_driver_sql(
            "CREATE TRIGGER fictional_accrual AFTER INSERT ON pg_temp.business_document_accruals "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_document_accruals VALUES (:id,:ledger,1)"),
            dict(id=uuid4(), ledger=header["ledger_id"]),
        )
        with Operations.context(MigrationContext.configure(c)):
            document_accrual_member_migration()["downgrade"]()
    assert (
        err.value.orig.diag.constraint_name == "ck_document_accrual_downgrade"
        and snapshot(s) == before
    )
