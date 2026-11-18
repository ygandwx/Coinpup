"""Allocation and return history keep the exact document source reviewed for each allocation."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_document_facts import allocation_document_facts
from coinpup_api.business.allocation_return_sources import original_sources
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate, BusinessPaymentCreate
from coinpup_api.business.settlements import BusinessSettlementService
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import setup_return
from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import allocation_document_fact_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_original_allocation_sources_remain_scoped_and_match_public_detail(drafts):
    s = drafts
    _, receipt, allocation, payload = setup_return(s, asset="EUR", count=2)
    before = snapshot(s)
    detail = BusinessSettlementReads(s["engine"]).get_allocation(
        s["owner"], s["ledger"], allocation.id
    )
    assert detail.receipt == receipt
    with Session(s["engine"]) as db:
        rows = original_sources(db, s["ledger"], payload)
        by_id = {r.id: r for r in detail.lines}
        for mapping, _, doc, source, _ in rows:
            assert mapping.document_revision_line_id is None
            assert (doc.asset_id, source.project_id) == (
                by_id[mapping.id].document_asset_id,
                by_id[mapping.id].project_id,
            )
            facts = allocation_document_facts(
                s["ledger"], allocation_id=allocation.id, line_ids=[mapping.id]
            )
            pinned = db.execute(select(facts)).one()
            assert (pinned.description, pinned.total_amount, pinned.asset_id) == (
                by_id[mapping.id].description,
                Decimal("100"),
                "EUR",
            )
            for ledger, group, ids in (
                (uuid4(), allocation.id, None),
                (s["ledger"], uuid4(), [mapping.id]),
                (s["ledger"], None, []),
                (s["ledger"], None, None),
            ):
                scope = allocation_document_facts(ledger, allocation_id=group, line_ids=ids)
                assert db.execute(select(scope)).first() is None
    assert snapshot(s) == before


def test_allocation_pins_replacement_and_keeps_it_after_later_revisions(revision):
    s, header, doc, _, origin = revision
    payment = BusinessPaymentCreate(
        id=uuid4(),
        party_id=doc.party_id,
        direction="receipt",
        account_id=s["account"].id,
        asset_id=doc.asset_id,
        amount="300",
        transaction_date=doc.issue_date,
        description="Fictional allocation source history",
    )
    BusinessSettlementService(s["engine"]).pay(
        s["owner"], s["ledger"], payment, "fictional-fact-pay"
    )
    day = doc.issue_date + timedelta(days=2)
    header = header | dict(issue_date=day, idempotency_key="fictional-reviewed-source")
    command = BusinessAllocationCreate(
        payment_id=payment.id,
        id=uuid4(),
        allocation_date=day,
        lines=[
            dict(
                document_id=doc.id,
                document_line_id=doc.lines[0].id,
                payment_amount="0.01",
                document_amount="0.01",
            )
        ],
    )
    earlier = command.model_copy(update={"id": uuid4(), "allocation_date": doc.issue_date})
    BusinessSettlementService(s["engine"]).allocate(
        s["owner"], s["ledger"], earlier, "fictional-before-document-correction"
    )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        first = append_accrual(
            c,
            header,
            doc,
            origin.postings[0],
            "correct",
            2,
            fact_changes=dict(
                description="Fictional reviewed text", project_id=None, project_snapshot=None
            ),
        )
        c.execute(select(func.coinpup_validate_operation_history(first["operation_id"])))
        service = BusinessSettlementService(c)
        result = service.allocate(s["owner"], s["ledger"], command, "fictional-pinned")
        assert result.id == command.id
        stored = (
            c.execute(
                select(BusinessAllocationLine.__table__).where(
                    BusinessAllocationLine.allocation_id == command.id
                )
            )
            .mappings()
            .one()
        )
        assert stored["document_revision_line_id"] == first["replacement_fact_id"]
        with pytest.raises(IntegrityError) as forged, c.begin_nested():
            c.execute(
                insert(BusinessAllocationLine),
                dict(stored) | dict(id=uuid4(), document_revision_line_id=uuid4()),
            )
        assert forged.value.orig.diag.constraint_name == "ck_allocation_document_fact"
        later = cancelled(header) | dict(
            id=uuid4(),
            previous_revision_id=header["id"],
            previous_version=header["document_version"],
            document_version=header["document_version"] + 1,
            idempotency_key="fictional-closed-source",
        )
        # Inspect the immutable source even in the presence of a staged terminal successor.
        append_accrual(c, later, doc, SimpleNamespace(**first), "cancel", 3)
        c.execute(select(func.coinpup_validate_operation_history(first["operation_id"])))
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_document_accrual_journals,tr_document_accrual_facts,"
            "tr_document_accrual_head,tr_journals_reversal_dimensions IMMEDIATE"
        )
        old = c.execute(
            select(allocation_document_facts(s["ledger"], allocation_id=earlier.id))
        ).one()
        assert (old.document_version, old.issue_date, old.description, old.project_id) == (
            origin.document_version,
            doc.issue_date,
            doc.lines[0].description,
            doc.lines[0].project_id,
        )
        assert service.allocate(s["owner"], s["ledger"], command, "fictional-pinned") == result
        history = allocation_document_facts(s["ledger"], allocation_id=command.id)
        row = c.execute(select(history)).one()
        assert (
            row.allocation_line_id,
            row.document_version,
            row.description,
            row.project_id,
            row.issue_date,
        ) == (stored["id"], header["document_version"], "Fictional reviewed text", None, day)
        payload = SimpleNamespace(
            payment_id=payment.id,
            lines=[SimpleNamespace(source_allocation_line_id=stored["id"], expected_version=1)],
        )
        mapping, _, source_doc, source_line, _ = original_sources(db, s["ledger"], payload)[0]
        assert (mapping.id, source_doc.issue_date, source_line.id, source_line.project_id) == (
            stored["id"],
            day,
            doc.lines[0].id,
            None,
        )
        assert (source_line.net_amount, source_line.tax_amount, source_line.total_amount) == (
            Decimal(doc.lines[0].net_amount),
            Decimal(doc.lines[0].tax_amount),
            Decimal(doc.lines[0].total_amount),
        )
        with pytest.raises(IntegrityError) as blocked, c.begin_nested():
            c.execute(insert(BusinessAllocationLine), dict(stored) | dict(id=uuid4()))
        assert blocked.value.orig.diag.constraint_name == "ck_allocation_document_fact"
        with pytest.raises(IntegrityError) as blocked, c.begin_nested():
            with Operations.context(MigrationContext.configure(c)):
                allocation_document_fact_migration()["downgrade"]()
        assert blocked.value.orig.diag.constraint_name == "ck_allocation_document_downgrade"
        tx.rollback()
    assert snapshot(s) == before
