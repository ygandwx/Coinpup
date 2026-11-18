"""Original 201 evidence retains its reviewed version, asset and bytes after later changes."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_hash import credit_hash_v2
from coinpup_api.business.credit_reads import summaries
from coinpup_api.business.credit_receipts import response_from_rows
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.models import Base
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_fact_validation import origin, pending_origin
from tests.integration.business.test_credit_pinned_validation import terminal
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_document_origin_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def all_rows(c):
    return {
        name: c.execute(select(table).order_by(*table.primary_key.columns)).mappings().all()
        for name, table in sorted(Base.metadata.tables.items())
    }


@pytest.mark.parametrize(
    "asset,unit,total",
    [
        ("BTC", "0.00000001", "0.00000003"),
        ("ETH", "0.000000000000000001", "0.000000000000000003"),
    ],
)
def test_versioned_original_proof_and_service_replay_keep_exact_asset_after_cancellation(
    sources, asset, unit, total
):
    s, h, doc, confirmed, original, _, payload = sources
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {asset}}
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c, Session(c) as db:
        h = h | {"asset_id": asset}
        first = append_accrual(
            c,
            h,
            doc,
            confirmed.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price=total,
                discount_amount="0",
                tax_rate_percent="0",
                net_amount=total,
                tax_amount="0",
                total_amount=total,
                project_id=None,
                project_snapshot=None,
            ),
        )
        new_payload = payload.model_copy(
            update={
                "id": uuid4(),
                "expected_version": h["document_version"],
                "lines": [
                    payload.lines[0].model_copy(update={"net_amount": unit, "tax_amount": "0"})
                ],
            }
        )
        identifier = pending_origin(
            c,
            original,
            identifier=new_payload.id,
            header_changes={"source_version": h["document_version"]},
            line_changes=dict(net_amount=unit, tax_amount="0", total_amount=unit),
            journal_line_changes=lambda row: dict(
                asset_id=asset, project_id=None, amount=Decimal(unit).copy_sign(row["amount"])
            ),
            receipt_changes={"request_hash": credit_hash_v2(s["ledger"], new_payload)},
        )
        origin(c, original.id)
        origin(c, identifier)
        saved = response_from_rows(db, identifier)
        assert saved.asset_id == asset and saved.source_version == h["document_version"]
        assert Decimal(saved.postings[0].total_amount) == Decimal(unit)
        with c.begin_nested() as date_settings:
            for style in ("German, DMY", "SQL, MDY", "Postgres, DMY"):
                c.execute(select(func.set_config("DateStyle", style, True)))
                assert c.scalar(
                    select(func.coinpup_business_credit_response(identifier))
                ) == saved.model_dump(mode="json")
                origin(c, identifier)
            date_settings.rollback()
        assert response_from_rows(db, original.id) == original
        assert summaries(db, s["ledger"], identifier=identifier)[0][1].asset_id == asset
        assert summaries(db, s["ledger"], identifier=original.id)[0][1].asset_id == "USD"
        assert summaries(db, uuid4(), identifier=identifier) == []
        service = BusinessCreditService(c)
        for closed in (False, True):
            if closed:
                append_accrual(c, terminal(h), doc, SimpleNamespace(**first), "cancel", 3)
            snapshot_before = all_rows(c)
            assert service.create(s["owner"], s["ledger"], new_payload, str(identifier)) == saved
            assert service.create(s["owner"], s["ledger"], payload, "fictional-credit") == original
            origin(c, identifier)
            assert all_rows(c) == snapshot_before
        # History proof and replay do not authorize this unfinished document group to commit.
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failure.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert credit(s, payload) == original and snapshot(s) == before


def test_empty_origin_migration_round_trip_preserves_legacy_receipts_and_functions(sources):
    s, _, _, _, original, _, payload = sources
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        migration = credit_document_origin_migration()
        migration["downgrade"]()
        origin(c, original.id)
        migration["upgrade"]()
        assert definitions(c) == baseline
        origin(c, original.id)
    assert credit(s, payload) == original and snapshot(s) == before


def test_versioned_origin_proof_cannot_be_downgraded_after_document_history(sources):
    s, h, doc, confirmed, _, _, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        append_accrual(c, h, doc, confirmed.postings[0], "correct", 2)
        with Operations.context(MigrationContext.configure(c)):
            credit_document_origin_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_document_origin_downgrade"
    assert snapshot(s) == before
