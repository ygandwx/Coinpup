"""At the closed storage stage a real service call never renders or leaves any issuance."""

import asyncio
import hashlib

import pytest
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_issuance_service import InvoiceIssuanceService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError

from tests.integration.business import test_invoice_snapshot_binding as binding
from tests.integration.business.test_invoice_profile_storage import file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_render_sources import PNG
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_closed_real_reservation_never_reaches_rendering_or_writes_a_false_receipt(
    company, tmp_path, monkeypatch
):
    s = company
    store = FileStore(tmp_path / "private", 8 * 1024 * 1024, 5)

    async def chunks():
        yield PNG

    staged = asyncio.run(store.stage(chunks(), len(PNG)))
    logo = file(
        s,
        byte_size=len(PNG),
        sha256=hashlib.sha256(PNG).hexdigest(),
        blob_key=store.publish(staged),
    )
    monkeypatch.setattr(binding, "file", lambda scope: logo)
    row = binding.reviewed(s, 200, logo=True, uppercase=True)
    command = InvoiceIssuanceCreate.model_validate_json(row["request_json"])
    service = InvoiceIssuanceService(s["engine"], store=store, font_directory=tmp_path / "fonts")
    before = snapshot(s)
    stored = {
        p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()
    }
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
        assert {
            p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()
        } == stored
