"""Real pinned renderer and PostgreSQL 17. Uses only disposable owned company facts."""

import asyncio
import os
from pathlib import Path

import pytest
from coinpup_api.business.invoice_issuance_reads import InvoiceIssuanceReads
from coinpup_api.business.invoice_issuance_service import InvoiceIssuanceService
from coinpup_api.files.models import StoredFile
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import select

from tests.integration.business.test_invoice_predecessor import advance, next_input
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_reservation_command import args
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.conftest import structure_database as structure_database
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup


def issue(s, row, store, fonts):
    svc = InvoiceIssuanceService(s["engine"], store=store, font_directory=fonts)
    return asyncio.run(svc.issue(**args(s, row)))


def private_pair(s, store, result):
    from pypdf import PdfReader

    seen = {}
    with s["engine"].connect() as c:
        for item in result.artifacts:
            key = c.scalar(
                select(StoredFile.blob_key).where(
                    StoredFile.id == item.file_id,
                    StoredFile.ledger_id == s["ledger"],
                    StoredFile.created_by == s["owner"],
                )
            )
            with store.open_blob(key, item.sha256, item.byte_size) as f:
                pages = PdfReader(f, strict=True).pages
                text = "\n".join(p.extract_text() for p in pages)
                assert result.number in text
                assert ("商业发票" if item.language == "zh" else "COMMERCIAL INVOICE") in text
                print(
                    f"{result.number} {item.language}: {len(pages)} pages, "
                    f"{item.byte_size} bytes, sha256 {item.sha256}."
                )
                f.seek(0)
                seen[item.language] = f.read()
    assert set(seen) == {"zh", "en"} and seen["zh"] != seen["en"]
    return seen


@pytest.mark.integration
def test_200_line_render_failure_recovers_same_pending_number_then_replays_exact_originals(
    company, tmp_path
):
    s = company
    row = reviewed(s, 200, uppercase=True)
    store = FileStore(tmp_path / "private", 8 * 1024 * 1024, 20)
    with pytest.raises(LedgerError) as stopped:
        issue(s, row, store, tmp_path / "missing-fonts")
    assert stopped.value.status == 503
    pending = InvoiceIssuanceReads(s["engine"]).get(s["owner"], s["ledger"], row["id"])
    before = snapshot(s)
    assert pending.number == row["number"] and pending.state == "pending" and not pending.artifacts
    assert (
        before["business_invoice_completions"] == []
        and len(before["business_invoice_reservations"]) == 1
    )
    result = issue(s, row, store, Path(os.environ["COINPUP_INVOICE_FONT_DIR"]))
    assert (
        result.id == pending.id
        and result.number == pending.number
        and result.snapshot_sha256 == pending.snapshot_sha256
    )
    saved = private_pair(s, store, result)
    done = snapshot(s)
    assert len(done["business_invoice_completions"]) == 1
    assert before["business_invoice_reservations"] == done["business_invoice_reservations"]
    assert {t for t in before if before[t] != done[t]} == {
        "business_invoice_completions",
        "change_log",
        "stored_files",
    }
    assert issue(s, row, store, tmp_path / "missing-fonts") == result
    assert private_pair(s, store, result) == saved and snapshot(s) == done


@pytest.mark.integration
def test_reissue_after_real_correction_keeps_old_number_and_every_original_byte(company, tmp_path):
    s = company
    row = reviewed(s)
    fonts = Path(os.environ["COINPUP_INVOICE_FONT_DIR"])
    store = FileStore(tmp_path / "private", 8 * 1024 * 1024, 20)
    old = issue(s, row, store, fonts)
    old_content = private_pair(s, store, old)
    advance(s, row["document_id"])
    successor = next_input(s, row, 2)
    new = issue(s, successor, store, fonts)
    assert new.previous_issuance_id == old.id and new.number != old.number and new.sequence == "2"
    assert {a.file_id for a in new.artifacts}.isdisjoint(a.file_id for a in old.artifacts)
    new_content = private_pair(s, store, new)
    assert all(new_content[lang] != old_content[lang] for lang in ("zh", "en"))
    before = snapshot(s)
    assert issue(s, row, store, tmp_path / "never-render") == old
    assert issue(s, successor, store, tmp_path / "never-render") == new
    assert private_pair(s, store, old) == old_content and private_pair(s, store, new) == new_content
    assert snapshot(s) == before
