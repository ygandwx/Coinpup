"""At the closed storage stage a real service call never renders or leaves any issuance."""

import asyncio

import pytest
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_issuance_service import InvoiceIssuanceService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError

from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_closed_real_reservation_never_reaches_rendering_or_writes_a_false_receipt(
    company, tmp_path
):
    s = company
    row = reviewed(s, 200, logo=True, uppercase=True)
    command = InvoiceIssuanceCreate.model_validate_json(row["request_json"])
    store = FileStore(tmp_path / "private", 8 * 1024 * 1024, 5)
    service = InvoiceIssuanceService(s["engine"], store=store, font_directory=tmp_path / "fonts")
    before = snapshot(s)
    for _ in range(2):
        with pytest.raises(LedgerError) as caught:
            asyncio.run(
                service.issue(
                    s["owner"],
                    s["ledger"],
                    command,
                    row["idempotency_key"],
                    raw_body=row["request_json"].encode(),
                )
            )
        assert caught.value.code == "constraint_conflict"
        assert (
            caught.value.__context__.orig.diag.constraint_name == "ck_invoice_reservation_complete"
        )
        assert snapshot(s) == before
        assert not store.root.exists()
