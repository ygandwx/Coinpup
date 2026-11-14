"""The bundle fixture preserves each version, monetary effects, private links and change cursor."""

from pathlib import Path
from uuid import uuid4

import pytest
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_complete_refund_revision_fixture_replays_zero_writes_with_private_original(
    ledger_setup, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from _database_archive import read_target
    from check_backup_restore import change_state, snapshot
    from check_bundle_restore import fictional_pdf, upload_fixture
    from check_refund_revision_restore import seed_refund_revisions, verify_refund_revisions

    s = ledger_setup
    files = DocumentService(s["engine"])
    store = FileStore(tmp_path / "private", 1024 * 1024, 30)
    pdf = fictional_pdf("Fictional evidence for committed refund revision")
    evidence = seed_refund_revisions(s["engine"], s["owner"], files, store, pdf, upload_fixture)
    first, second, terminal, active = [r["result"] for r in evidence["revisions"]]
    assert [r.version for r in (first, second, terminal, active)] == [2, 3, 4, 2]
    assert first.previous.allocation_return_ids and first.replacement.allocation_return_ids == ()
    assert second.replacement.allocation_return_ids == first.previous.allocation_return_ids
    assert terminal.previous == second.replacement and terminal.replacement is None
    assert [fee.amount for fee in active.replacement.fees] == ["0.03", "0.02"]
    assert (
        evidence["expected"]["current"][1].fact.allocation_return_ids
        == active.replacement.allocation_return_ids
    )
    archive = evidence["originals"][0]
    assert archive["request"].operation_id == terminal.operation_id and evidence["links"]
    target = read_target("COINPUP_DATABASE_URL")
    assert target.database == s["engine"].url.database
    before = snapshot(target)
    changes = change_state(s["engine"], s["owner"])
    assert {row["entity_type"] for row in changes[0]} >= {
        "business_refund_revisions",
        "business_refund_revision_returns",
        "business_refunds",
    }
    verify_refund_revisions(s["engine"], evidence)
    record, key = files.get_download(s["owner"], evidence["ledger"], archive["receipt"].file_id)
    with store.open_blob(key, record.sha256, record.byte_size) as fp:
        assert fp.read() == pdf
    assert (
        upload_fixture(
            files,
            store,
            s["owner"],
            evidence["ledger"],
            pdf,
            archive["request"].original_filename,
            request=archive["request"],
        )["receipt"]
        == archive["receipt"]
    )
    with pytest.raises(LedgerError) as err:
        files.get_download(s["owner"], uuid4(), archive["receipt"].file_id)
    assert err.value.status == 404
    assert snapshot(target) == before and change_state(s["engine"], s["owner"]) == changes
