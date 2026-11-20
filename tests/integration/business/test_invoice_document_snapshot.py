"""Freeze real committed versions without issuing, repricing or modifying any database row."""

from uuid import uuid4

import pytest
from coinpup_api.business.document_current import BusinessDocumentCurrentReads
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.invoice_document_snapshot import freeze_invoice_document
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError

from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("count", [2, 200])
def test_real_current_snapshot_survives_correction_and_cancel_without_changing_any_table(
    drafts, count
):
    s = drafts
    document, _ = confirmed(s, "invoice", count)
    reads = BusinessDocumentCurrentReads(s["engine"])
    intent = InvoiceIssuanceCreate(
        id=uuid4(),
        document_id=document.id,
        expected_document_version=document.version,
        configuration_id=uuid4(),
        expected_configuration_version=1,
        previous_issuance_id=None,
    )

    def read():
        return reads.current(s["owner"], s["ledger"], document.id)

    def freeze(current, command):
        return freeze_invoice_document(
            current, ASSETS["USD"], ledger_id=s["ledger"], command=command
        )

    before = snapshot(s)
    original = read()
    first = freeze(original, intent)
    assert first.decode()["current"] == original.model_dump(mode="json")
    assert len(first.decode()["current"]["document"]["lines"]) == count
    assert snapshot(s) == before
    changed = correction(document).model_copy(
        update={"replacement": replacement(document, {document.lines[-1].id: {"unit_price": "0"}})}
    )
    receipt = send(s, document, changed, "fictional-invoice-snapshot-correct")
    before = snapshot(s)
    current = read()
    with pytest.raises(MoneyError):
        freeze(current, intent)
    second = freeze(
        current, intent.model_copy(update={"expected_document_version": receipt.version})
    )
    rows = second.decode()["current"]["document"]["lines"]
    assert len(rows) == count and rows[-1]["total_amount"] == "0.00"
    assert len(second.decode()["current"]["postings"]) == count - 1
    assert second.decode()["current"] == current.model_dump(mode="json")
    assert first.decode()["current"] == original.model_dump(mode="json")
    assert first.sha256 != second.sha256
    assert snapshot(s) == before
    ended = send(
        s,
        document,
        ConfirmedDocumentCancellation(
            expected_version=receipt.version, reason="Fictional invoice snapshot cancellation"
        ),
        "fictional-invoice-snapshot-cancel",
    )
    before = snapshot(s)
    with pytest.raises(MoneyError):
        freeze(read(), intent.model_copy(update={"expected_document_version": ended.version}))
    assert send(s, document, changed, "fictional-invoice-snapshot-correct") == receipt
    assert first.decode()["current"] == original.model_dump(mode="json")
    assert second.decode()["current"] == current.model_dump(mode="json")
    assert snapshot(s) == before
