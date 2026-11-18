"""A returned allocation retains its own document asset, original journal and reviewed details."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_posting_facts import return_posting_facts
from coinpup_api.business.allocation_return_reads import member_details
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import AssetRecord
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_document_facts import source_payment
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_revision_service import cancel, command
from tests.integration.conftest import return_document_source_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_usd_payment_return_preserves_three_satoshi_document_source_across_versions(revision):
    s, h, doc, _, origin = revision
    payment = source_payment(s, doc)
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {"BTC"}}
        )
    before = snapshot(s)
    h = h | dict(asset_id="BTC", idempotency_key="fictional-cross-asset-document")
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        priced = append_accrual(
            c,
            h,
            doc,
            origin.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price="0.00000003",
                tax_rate_percent="0",
                discount_amount="0",
                net_amount="0.00000003",
                tax_amount="0",
                total_amount="0.00000003",
                project_id=None,
                project_snapshot=None,
                description="Fictional three satoshi",
            ),
        )
        c.execute(select(func.coinpup_validate_operation_history(priced["operation_id"])))
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_document_accrual_journals,tr_document_accrual_facts,"
            "tr_document_accrual_head,tr_journals_reversal_dimensions IMMEDIATE"
        )
        allocation = BusinessAllocationCreate(
            id=uuid4(),
            payment_id=payment.id,
            allocation_date=doc.issue_date,
            lines=[
                dict(
                    document_id=doc.id,
                    document_line_id=doc.lines[0].id,
                    payment_amount="0.03",
                    document_amount="0.00000003",
                )
            ],
        )
        BusinessSettlementService(c).allocate(
            s["owner"], s["ledger"], allocation, "fictional-satoshis"
        )
        c.execute(select(func.coinpup_validate_business_allocation(allocation.id)))
        mapping = c.scalar(
            select(BusinessAllocationLine.id).where(
                BusinessAllocationLine.allocation_id == allocation.id
            )
        )
        body = BusinessAllocationReturnCreate(
            id=uuid4(),
            payment_id=payment.id,
            return_date=doc.issue_date,
            reason="Fictional single satoshi",
            lines=[
                dict(
                    source_allocation_line_id=mapping,
                    expected_version=1,
                    payment_amount="0.01",
                    document_amount="0.00000001",
                )
            ],
        )
        service = BusinessAllocationReturnService(c)
        first = service.create(s["owner"], s["ledger"], body, "fictional-one-satoshi")
        c.execute(select(func.coinpup_validate_operation_history(first.postings[0].operation_id)))
        c.execute(select(func.coinpup_validate_business_allocation_return(first.id)))
        details = member_details(db, s["ledger"], first.id, first, db.get(AssetRecord, "USD"))
        assert [
            (row.description, row.document_asset_id, row.document_amount) for row in details
        ] == [("Fictional three satoshi", "BTC", "0.00000001")]
        revised = BusinessAllocationReturnRevisionService(c)
        change = command(
            body,
            day=doc.issue_date,
            lines=[
                body.lines[0].model_copy(
                    update={"payment_amount": "0.02", "document_amount": "0.00000002"}
                )
            ],
        )
        current = revised.correct(
            s["owner"], s["ledger"], first.id, change, "fictional-two-satoshis"
        )
        c.execute(select(func.coinpup_validate_operation_history(first.postings[0].operation_id)))
        assert current.members[0].document_asset_id == "BTC"
        ended = revised.cancel(
            s["owner"], s["ledger"], first.id, cancel(2), "fictional-cancel-satoshis"
        )
        assert ended.members[0].document_asset_id == "BTC"
        c.execute(select(func.coinpup_validate_business_allocation_return(first.id)))
        for version in (current, ended):
            c.execute(select(func.coinpup_validate_return_revision(version.revision_id)))
        facts = return_posting_facts(s["ledger"], return_ids=[first.id])
        assert set(
            db.execute(select(facts.c.group_version, facts.c.document_asset_id, facts.c.project_id))
        ) == {(1, "BTC", None), (2, "BTC", None)}
        assert service.create(s["owner"], s["ledger"], body, "fictional-one-satoshi") == first
        assert (
            revised.correct(s["owner"], s["ledger"], first.id, change, "fictional-two-satoshis")
            == current
        )
        assert (
            revised.cancel(
                s["owner"], s["ledger"], first.id, cancel(2), "fictional-cancel-satoshis"
            )
            == ended
        )
        with pytest.raises(IntegrityError) as protected, c.begin_nested():
            with Operations.context(MigrationContext.configure(c)):
                return_document_source_migration()["downgrade"]()
        assert protected.value.orig.diag.constraint_name == "ck_return_document_source_downgrade"
        tx.rollback()
    assert snapshot(s) == before


def test_all_four_actual_return_definitions_survive_an_empty_migration_round_trip(revision):
    s, *_ = revision
    sql = return_document_source_migration()
    signatures = {
        "posting": "coinpup_return_posting_facts(uuid,uuid[],uuid[])",
        "sequence": "coinpup_validate_return_source_sequences(uuid,uuid[])",
        "history": "coinpup_validate_return_source_history(uuid,uuid,uuid)",
        "receipt": "coinpup_business_return_revision_response(uuid)",
    }
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):

        def definitions():
            return {
                name: c.scalar(
                    text("SELECT pg_get_functiondef(to_regprocedure(:name))"),
                    {"name": "public." + signature},
                )
                for name, signature in signatures.items()
            }

        actual = definitions()
        assert actual == {key: sql["_" + key.upper()] for key in signatures}
        sql["downgrade"]()
        assert definitions() == sql["_OLD"]
        sql["upgrade"]()
        assert definitions() == actual
