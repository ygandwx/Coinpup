"""Plan from real sealed invoice and bill postings without changing the current or fixed receipt."""

import pytest
from coinpup_api.business.document_revision_plan import (
    SealedAccrual,
    plan_document_accrual_revision,
)
from coinpup_api.ledger.assets import ASSETS

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_document_revision_capacity import confirmed, replacement
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_real_confirmation_keeps_its_exact_dates_posting_and_original_receipt(drafts, kind):
    s = drafts
    saved, intent = prepare(s, kind=kind)
    receipt = submit(s, saved, intent)
    original = confirmed(s, saved.id)
    postings = tuple(
        SealedAccrual(s["ledger"], saved.id, p.line_id, p.operation_id, 1, p.journal_id)
        for p in receipt.postings
    )
    before = snapshot(s)
    proposed = replacement(
        original,
        {
            original.lines[0].id: {
                "quantity": "1",
                "unit_price": "0.03",
                "discount_amount": "0",
                "tax_rate_percent": "50",
            }
        },
        asset_id="EUR",
    )
    plan = plan_document_accrual_revision(
        original, ASSETS["USD"], postings, proposed, ASSETS["EUR"]
    )
    assert (plan.previous_version, plan.version) == (
        receipt.document_version,
        receipt.document_version + 1,
    )
    (change,) = plan.lines
    assert change.previous_posting.journal_id == receipt.postings[0].journal_id
    assert change.next_operation_version == 2 and change.posting_action == "correct"
    assert (change.accrual.price.net.minor_units, change.accrual.price.tax.minor_units) == (3, 2)
    assert change.accrual.account_amount.minor_units == (5 if kind == "invoice" else -5)
    assert change.accrual.account_amount.asset == ASSETS["EUR"]
    assert submit(s, saved, intent) == receipt
    assert confirmed(s, saved.id) == original and snapshot(s) == before
