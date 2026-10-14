"""Fictional original payment evidence remains reproducible after archival and period close."""

from pathlib import Path

import pytest
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_same_and_cross_ledger_bank_evidence_replay_changes_no_table_after_archive(
    ledger_setup, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_bundle_restore import fictional_pdf, upload_fixture
    from check_payment_evidence_restore import rows, seed_payment_evidence, verify_payment_evidence

    s = ledger_setup
    store = FileStore(tmp_path / "private", 1024 * 1024, 30)
    content = fictional_pdf("Fictional bank statement 2026-09-30 USD 100.00")
    evidence = seed_payment_evidence(
        s["engine"], s["owner"], DocumentService(s["engine"]), store, content, upload_fixture
    )
    assert len(evidence["confirmations"]) == 2
    assert evidence["confirmations"][0][0] != evidence["confirmations"][1][0]
    assert evidence["payment"].initial_allocation is not None
    assert len(evidence["payment"].initial_allocation.postings) == 1
    before = rows(s["engine"])
    verify_payment_evidence(s["engine"], evidence)
    assert rows(s["engine"]) == before
    files = DocumentService(s["engine"])
    for original in evidence["originals"]:
        metadata, key = files.get_download(
            s["owner"], original["ledger"], original["receipt"].file_id
        )
        with store.open_blob(key, metadata.sha256, metadata.byte_size) as source:
            assert source.read() == content
