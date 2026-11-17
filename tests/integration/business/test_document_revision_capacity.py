"""Fictional B-11. Compare each current dependency with its own document-side line."""

from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.document_revision_capacity import check_document_revision_capacity
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentReplacement
from coinpup_api.business.draft_schemas import BusinessDraftLineInput, DraftHeaderInput
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_revision_service import command
from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import allocate
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def confirmed(s, identifier):
    return (
        BusinessConfirmationReads(s["engine"])
        .get_confirmation(s["owner"], s["ledger"], identifier)
        .document
    )


def replacement(doc, changes=None, **header):
    changes = changes or {}
    data = doc.model_dump(include=set(DraftHeaderInput.model_fields)) | header
    return ConfirmedDocumentReplacement(
        **data,
        lines=[
            row.model_dump(include=set(BusinessDraftLineInput.model_fields))
            | changes.get(row.id, {})
            for row in doc.lines
            if row.id not in changes or changes[row.id] is not None
        ],
    )


def check(s, doc, proposed):
    with Session(s["engine"]) as db:
        return check_document_revision_capacity(
            db,
            s["ledger"],
            doc,
            ASSETS[doc.asset_id],
            proposed,
            ASSETS[proposed.asset_id] if proposed else None,
        )


def blocked(s, doc, proposed):
    with pytest.raises(LedgerError) as exc:
        check(s, doc, proposed)
    assert (exc.value.status, exc.value.code) == (409, "document_revision_dependency")


@pytest.mark.parametrize("direction", ["payout", "receipt"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_effective_100_needs_explicit_return_before_50_is_allowed(drafts, asset, direction):
    s = drafts
    _, _, allocation, body = setup_return(s, direction=direction, asset=asset)
    doc = confirmed(s, allocation.lines[0].document_id)
    source = doc.lines[0].id
    half = replacement(doc, {source: {"unit_price": "50.00"}})
    before = snapshot(s)
    blocked(s, doc, half)
    blocked(s, doc, replacement(doc, issue_date="2026-10-01"))
    blocked(s, doc, None)
    check(s, doc, replacement(doc))
    assert snapshot(s) == before
    returned = body.model_copy(
        update={
            "lines": [
                body.lines[0].model_copy(
                    update={
                        "payment_amount": "55" if asset == "EUR" else "50",
                        "document_amount": "50",
                    }
                )
            ]
        }
    )
    submit_return(s, returned)
    before = snapshot(s)
    check(s, doc, half)
    blocked(s, doc, replacement(doc, issue_date="2026-10-01"))
    blocked(s, doc, replacement(doc, {source: {"unit_price": "49.99"}}))
    blocked(s, doc, replacement(doc, party_id=uuid4()))
    blocked(s, doc, replacement(doc, asset_id="GBP"))
    blocked(s, doc, replacement(doc, {source: {"project_id": uuid4()}}))
    # Allocations carry the party and project. Only credits classify the source expense/income.
    check(s, doc, replacement(doc, {source: {"category_id": uuid4()}}))
    assert snapshot(s) == before
    submit_return(s, returned.model_copy(update={"id": uuid4()}), "fictional-return-final")
    before = snapshot(s)
    check(s, doc, None)
    check(s, doc, replacement(doc, party_id=uuid4(), asset_id="GBP", issue_date="2026-10-01"))
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_credit_components_and_positions_cannot_borrow_from_other_sources(drafts, kind):
    s = drafts
    saved, _, _, payload = setup_credit(s, kind=kind, count=3)
    receipt = credit(s, payload)
    doc = confirmed(s, saved.id)
    line_id = doc.lines[1].id
    before = snapshot(s)
    blocked(s, doc, replacement(doc, {line_id: {"unit_price": "17.22", "tax_rate_percent": "100"}}))
    blocked(s, doc, replacement(doc, {line_id: {"unit_price": "27.64", "tax_rate_percent": "10"}}))
    blocked(s, doc, replacement(doc, {line_id: {"category_id": uuid4()}}))
    blocked(s, doc, replacement(doc, {line_id: None}))
    blocked(s, doc, None)
    check(s, doc, replacement(doc, {line_id: {"unit_price": "27.70", "tax_rate_percent": "10"}}))
    assert snapshot(s) == before
    check(s, doc, replacement(doc, issue_date="2026-10-21"))
    blocked(s, doc, replacement(doc, issue_date="2026-10-22"))
    command(s, {"original": receipt}, [row.id for row in doc.lines])
    check(s, doc, replacement(doc, issue_date="2026-11-06"))
    blocked(s, doc, replacement(doc, issue_date="2026-11-07"))
    command(s, {"original": receipt}, None, version=2)
    before = snapshot(s)
    check(s, doc, replacement(doc, {line_id: None}, issue_date="2026-11-07"))
    check(s, doc, None)
    assert snapshot(s) == before


def test_scoped_sources_use_one_totals_read_and_reject_mixed_ledgers(drafts):
    s = drafts
    saved, _, _, payload = setup_credit(s, count=6)
    credit(s, payload)
    doc = confirmed(s, saved.id)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        check(s, doc, replacement(doc))
        assert len(statements) == 1
        foreign = doc.model_copy(
            update={
                "lines": [doc.lines[0].model_copy(update={"ledger_id": uuid4()}), *doc.lines[1:]]
            }
        )
        with pytest.raises(LedgerError) as exc:
            check(s, foreign, replacement(doc))
        assert exc.value.code == "document_revision_integrity"
        assert len(statements) == 1
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)


def test_net_applied_and_effective_credit_share_the_same_gross_line_capacity(drafts):
    s = drafts
    _, _, allocation, body = setup_return(s, direction="receipt")
    doc = confirmed(s, allocation.lines[0].document_id)
    source = doc.lines[0].id
    submit_return(s, body)
    request = BusinessCreditCreate(
        id=uuid4(),
        document_id=doc.id,
        expected_version=doc.version,
        credit_date="2026-10-02",
        reason="Fictional credit on a partially returned invoice",
        lines=[
            dict(
                source_line_id=source,
                net_amount="12.34",
                tax_amount="0",
                recognition_date="2026-10-02",
            )
        ],
    )
    receipt = credit(s, request)
    before = snapshot(s)
    # 100 original, minus 20 returned, plus 12.34 credited. Each alone fits 92.33.
    blocked(s, doc, replacement(doc, {source: {"unit_price": "92.33"}}))
    check(s, doc, replacement(doc, {source: {"unit_price": "92.34"}}))
    assert snapshot(s) == before
    command(s, {"original": receipt}, None)
    check(s, doc, replacement(doc, {source: {"unit_price": "80.00"}}))


def test_fully_returned_old_allocation_leaves_only_the_new_application_date(drafts):
    s = drafts
    _, _, allocation, body = setup_return(s, full=True, asset="EUR")
    doc = confirmed(s, allocation.lines[0].document_id)
    submit_return(s, body)
    later = allocation.model_dump() | {"id": uuid4(), "allocation_date": "2026-10-15"}
    allocate(s, type(allocation).model_validate(later), "fictional-later-reapplication")
    before = snapshot(s)
    check(s, doc, replacement(doc, issue_date="2026-10-15"))
    blocked(s, doc, replacement(doc, issue_date="2026-10-16"))
    blocked(s, doc, replacement(doc, {doc.lines[0].id: {"unit_price": "99.99"}}))
    assert snapshot(s) == before
