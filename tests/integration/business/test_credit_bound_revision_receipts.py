"""Fixed 200 receipts retain their asset and quantities after later source changes."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_hash import credit_hash_v2
from coinpup_api.business.credit_receipts import response_from_rows as original_response
from coinpup_api.business.credit_revision_hash import credit_revision_hash_v2
from coinpup_api.business.credit_revision_receipts import response_from_rows
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
)
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.ledger.control_accounts import ControlAccounts
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_origin import all_rows
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_fact_validation import pending_fact, pending_origin
from tests.integration.business.test_credit_pinned_validation import terminal
from tests.integration.business.test_credit_revision_receipts import save_receipt
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_bound_receipt_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "asset,unit,total",
    [("BTC", "0.00000001", "0.00000003"), ("ETH", "0.000000000000000001", "0.000000000000000003")],
)
def test_fixed_bound_correction_and_actual_cancellation_replay_all_tables_unchanged(
    sources, asset, unit, total
):
    s, h, doc, confirmed, original, ch, payload = sources
    service = BusinessCreditRevisionService(s["engine"])
    service.cancel(
        s["owner"],
        s["ledger"],
        original.id,
        CreditCancellationCreate(expected_version=1, reason="Fictional explicit release"),
        "fictional-old-group-release",
    )
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {asset}}
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as gated, s["engine"].begin() as c, Session(c) as db:
        h = h | dict(asset_id=asset)
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
            ),
        )
        create = payload.model_copy(
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
            identifier=create.id,
            header_changes=dict(source_version=h["document_version"]),
            line_changes=dict(net_amount=unit, tax_amount="0", total_amount=unit),
            journal_line_changes=lambda row: dict(
                asset_id=asset, amount=Decimal(unit).copy_sign(row["amount"])
            ),
            receipt_changes=dict(request_hash=credit_hash_v2(s["ledger"], create)),
        )
        created = original_response(db, identifier)
        correction = CreditCorrectionCreate(
            expected_version=1,
            reason=ch["revision_reason"],
            replacement=dict(
                credit_date=doc.issue_date, reason=ch["credit_reason"], lines=create.lines
            ),
        )
        header = ch | dict(credit_id=identifier, idempotency_key="fictional-bound-correction")
        pending_fact(
            c,
            header,
            created,
            changes=dict(recognition_date=create.lines[0].recognition_date),
            journal_changes=dict(recognition_date=create.lines[0].recognition_date),
        )
        corrected = response_from_rows(db, header["id"])
        assert corrected.asset_id == asset and corrected.version == 2
        assert Decimal(corrected.members[0].total_amount) == Decimal(unit)
        save_receipt(
            c,
            header,
            corrected.model_dump(mode="json"),
            request_hash=credit_revision_hash_v2("correct", s["ledger"], identifier, correction),
        )
        c.execute(select(func.coinpup_validate_business_credit(identifier)))
        cancel = CreditCancellationCreate(expected_version=2, reason="Fictional bound terminal")
        revise = BusinessCreditRevisionService(c)
        ended = revise.cancel(s["owner"], s["ledger"], identifier, cancel, "fictional-bound-cancel")
        assert ended.asset_id == asset and ended.version == 3 and ended.action == "cancel"
        assert ended.members[0].net_amount is None
        assert Decimal(ended.members[0].previous_net_amount) == Decimal(unit)
        c.execute(select(func.coinpup_validate_business_credit(identifier)))
        append_accrual(c, terminal(h), doc, SimpleNamespace(**first), "cancel", 3)
        snapshot_before = all_rows(c)
        assert (
            revise.correct(
                s["owner"], s["ledger"], identifier, correction, "fictional-bound-correction"
            )
            == corrected
        )
        assert (
            revise.cancel(s["owner"], s["ledger"], identifier, cancel, "fictional-bound-cancel")
            == ended
        )
        assert (
            BusinessCreditService(c).create(s["owner"], s["ledger"], create, str(identifier))
            == created
        )
        assert response_from_rows(db, header["id"]) == corrected
        assert response_from_rows(db, ended.revision_id) == ended
        with c.begin_nested() as date_settings:
            for style in ("German, DMY", "SQL, MDY", "Postgres, DMY"):
                c.execute(select(func.set_config("DateStyle", style, True)))
                for receipt in (corrected, ended):
                    assert c.scalar(
                        select(func.coinpup_business_credit_revision_response(receipt.revision_id))
                    ) == receipt.model_dump(mode="json")
                    c.execute(
                        select(func.coinpup_validate_credit_revision_receipt(receipt.revision_id))
                    )
            date_settings.rollback()
        assert all_rows(c) == snapshot_before
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert gated.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert credit(s, payload) == original and snapshot(s) == before


def test_empty_bound_receipt_migration_round_trip_keeps_original_functions(sources):
    s, _, _, _, original, _, payload = sources
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        migration = credit_bound_receipt_migration()
        migration["downgrade"]()
        migration["upgrade"]()
        assert definitions(c) == baseline
    assert credit(s, payload) == original and snapshot(s) == before


def test_bound_receipt_migration_refuses_existing_document_history(sources):
    s, h, doc, confirmed, _, _, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        append_accrual(c, h, doc, confirmed.postings[0], "correct", 2)
        with Operations.context(MigrationContext.configure(c)):
            credit_bound_receipt_migration()["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_credit_bound_receipt_downgrade"
    assert snapshot(s) == before
