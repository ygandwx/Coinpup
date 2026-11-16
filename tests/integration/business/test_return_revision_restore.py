"""The private bundle fixture can reproduce all return and linked refund receipts after archive."""

from pathlib import Path

import pytest
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_archived_return_bundle_uses_real_routes_private_bytes_and_zero_write_replays(
    ledger_setup, authenticated_client, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_backup_restore import change_state
    from check_bundle_restore import fictional_pdf, upload_fixture
    from check_return_revision_restore import seed_return_revisions, verify_return_revisions

    s = ledger_setup
    client, _, _ = authenticated_client
    files = DocumentService(s["engine"])
    store = FileStore(tmp_path / "private", 1024 * 1024, 30)
    pdf = fictional_pdf("Fictional original of retired return member")
    evidence = seed_return_revisions(s["engine"], s["owner"], files, store, pdf, upload_fixture)
    ledger = evidence["ledger"]
    assert evidence["expected"]["refund_bindings"] == [
        (evidence["primary"], 2),
        (evidence["auxiliary"], 1),
    ]
    before = snapshot(s)
    cursor = change_state(s["engine"], s["owner"])
    verify_return_revisions(s["engine"], evidence)
    attachment = evidence["attachment"]["receipt"]
    metadata, key = files.get_download(s["owner"], ledger, attachment.file_id)
    with store.open_blob(key, metadata.sha256, metadata.byte_size) as source:
        assert source.read() == pdf
    with pytest.raises(LedgerError) as rejected:
        files.get_download(s["owner"], s["ledger"], attachment.file_id)
    assert (rejected.value.status, rejected.value.code) == (404, "not_found")
    for case, expected in evidence["cases"]:
        service, action, target, body, key, raw = case
        refund = expected.kind.startswith("business_refund")
        base = f"/api/v1/ledgers/{ledger}/" + (
            "business-refunds" if refund else "business-allocation-returns"
        )
        suffix = {"correct": "corrections", "cancel": "cancellations"}
        path = base if action == "create" else f"{base}/{target[0]}/{suffix[action]}"
        reply = client.post(
            path, content=raw, headers={"Content-Type": "application/json", "Idempotency-Key": key}
        )
        assert reply.status_code == (201 if action == "create" else 200), reply.text
        assert reply.headers["cache-control"] == "no-store" and reply.json() == expected.model_dump(
            mode="json"
        )
        if action != "create":
            assert (
                client.get(f"{base}/{target[0]}/revisions/{expected.revision_id}").json()
                == reply.json()
            )
    assert snapshot(s) == before and change_state(s["engine"], s["owner"]) == cursor
