"""Bind immutable financial receipts to their real member sets, in both directions."""

import json
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.confirmation_hash import document_revision_hash_v2
from coinpup_api.business.document_fixed_receipts import (
    document_receipt_anchor,
    fixed_document_response,
)
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.models import CommandReceipt
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_receipt_seal_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def receipt_values(db, h, doc):
    fixed = fixed_document_response(db, h["ledger_id"], doc.id, h["id"])
    cmd = ConfirmedDocumentCorrection(
        expected_version=h["previous_version"], reason=h["reason"], replacement=replacement(doc)
    )
    body = json.dumps(cmd.model_dump(mode="json", exclude_unset=True)).encode()
    return dict(
        ledger_id=h["ledger_id"],
        key=h["idempotency_key"],
        operation_id=document_receipt_anchor(fixed),
        hash_version=2,
        request_hash=document_revision_hash_v2(
            "correct", h["ledger_id"], doc.id, cmd, raw_body=body
        ),
        response_status=200,
        response=fixed.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "wrong-key",
        "wrong-anchor",
        "wrong-hash-version",
        "wrong-status",
        "omitted",
        "version-as-decimal",
        "changed-price-spelling",
        "wrong-document",
        "extra-field",
    ],
)
def test_receipt_and_revision_must_point_to_the_same_complete_evidence(revision, fault):
    s, h, doc, _, origin = revision
    h = h | dict(idempotency_key="fictional-seal-proof")
    if fault == "wrong-anchor":
        _, other = confirmed(s, "bill", 1, key="fictional-other-anchor")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        append_accrual(c, h, doc, origin.postings[0], "correct", 2)
        values = receipt_values(db, h, doc)
        response = values["response"]
        if fault == "wrong-key":
            values["key"] = "fictional-unrelated-key"
        elif fault == "wrong-anchor":
            values["operation_id"] = other[0].operation_id
        elif fault == "wrong-hash-version":
            values["hash_version"] = 1
        elif fault == "wrong-status":
            values["response_status"] = 201
        elif fault == "version-as-decimal":
            response["version"] = float(response["version"])
        elif fault == "changed-price-spelling":
            response["previous"]["lines"][0]["unit_price"] += "0"
        elif fault == "wrong-document":
            response["document_id"] = str(uuid4())
        elif fault == "extra-field":
            response["unexpected"] = True
        if fault != "omitted":
            c.execute(insert(CommandReceipt), values)
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_command_document_revision,zz_tr_document_revision_receipt IMMEDIATE"
        )
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failed.value.orig.diag.constraint_name == (
        "ck_document_revision_complete" if fault is None else "ck_document_revision_receipt"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("mutation", ["late-zero-line", "late-accrual", "downgrade"])
def test_fixed_receipt_closes_even_an_additional_zero_value_line(revision, mutation):
    s, h, doc, _, origin = revision
    h = h | dict(idempotency_key="fictional-close-members")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        row = append_accrual(c, h, doc, origin.postings[0], "correct", 2)
        c.execute(insert(CommandReceipt), receipt_values(db, h, doc))
        c.execute(
            select(
                func.coinpup_validate_document_revision_receipt(s["ledger"], h["idempotency_key"])
            )
        )
        if mutation == "late-zero-line":
            extra = line(h, doc) | dict(
                source_line_id=uuid4(),
                line_no=2,
                unit_price="-0",
                discount_amount="0",
                net_amount="0",
                tax_amount="0",
                total_amount="0",
            )
            c.execute(insert(Fact), extra)
        elif mutation == "late-accrual":
            c.execute(insert(Link), row | dict(id=uuid4()))
        else:
            with Operations.context(MigrationContext.configure(c)):
                document_receipt_seal_migration()["downgrade"]()
    assert failed.value.orig.diag.constraint_name == (
        "ck_document_revision_seal_downgrade"
        if mutation == "downgrade"
        else "ck_document_revision_sealed"
    )
    assert snapshot(s) == before
