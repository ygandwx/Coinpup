"""Committed source changes update capacity without rewriting a cancelled credit's history."""

import pytest
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.business.credit_revision_history_reads import BusinessCreditHistory
from coinpup_api.business.credit_revision_reads import BusinessCreditRevisionReads
from coinpup_api.business.credit_revision_schemas import CreditCancellationCreate
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.business.settlement_balances import BusinessSettlementBalances
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from sqlalchemy.orm import Session

from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
def test_actual_document_revisions_preserve_old_credit_currency_and_history(
    drafts, kind, asset, unit
):
    s = drafts
    saved, _, _, original = setup_credit(s, kind)
    created = credit(s, original)
    cancelled = BusinessCreditRevisionService(s["engine"]).cancel(
        s["owner"],
        s["ledger"],
        created.id,
        CreditCancellationCreate(expected_version=1, reason="Fictional explicit source release"),
        "fictional-old-credit-cancel",
    )
    with Session(s["engine"]) as db:
        doc = read_confirmed_document_version(db, s["ledger"], saved.id).document
    changed = send(
        s,
        doc,
        correction(doc).model_copy(
            update={
                "replacement": replacement(
                    doc,
                    {doc.lines[0].id: {"unit_price": unit, "tax_rate_percent": "0"}},
                    asset_id=asset,
                )
            }
        ),
        "fictional-new-currency",
    )
    reads, history, groups = (
        cls(s["engine"])
        for cls in (BusinessCreditReads, BusinessCreditHistory, BusinessCreditRevisionReads)
    )
    balances = BusinessSettlementBalances(s["engine"])
    before = snapshot(s)
    capacity = reads.capacity(s["owner"], s["ledger"], doc.id)
    assert (capacity.asset_id, capacity.expected_version) == (asset, changed.version)
    assert capacity.lines[0].available_gross_amount == unit
    assert balances.document_capacity(s["owner"], s["ledger"], doc.id).outstanding_amount == unit
    current = groups.current(s["owner"], s["ledger"], created.id)
    assert (current.asset_id, current.version, current.status) == ("USD", 2, "cancelled")
    assert (
        history.get_revision(s["owner"], s["ledger"], created.id, cancelled.revision_id)
        == cancelled
    )
    assert credit(s, original) == created
    assert snapshot(s) == before
    send(
        s,
        doc,
        ConfirmedDocumentCancellation(
            expected_version=changed.version, reason="Fictional terminal document"
        ),
        "fictional-later-document-cancel",
    )
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert groups.current(s["owner"], s["ledger"], created.id) == current
    assert (
        history.get_revision(s["owner"], s["ledger"], created.id, cancelled.revision_id)
        == cancelled
    )
    assert credit(s, original) == created
    assert len(history.list_history(s["owner"], s["ledger"], created.id)) == 2
    assert snapshot(s) == before
