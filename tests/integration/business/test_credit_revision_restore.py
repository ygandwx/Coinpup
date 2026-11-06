"""Real revision and private-file fixtures; full dump/restore is separately run by Compose."""

from pathlib import Path

import pytest
from coinpup_api.business.credit_models import BusinessCreditLine
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.models import CommandReceipt, Entity
from coinpup_api.ledger.period_models import LedgerPeriod
from sqlalchemy import func, select

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_closed_archived_credit_revisions_and_new_member_files_replay_without_any_write(
    ledger_setup, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_backup_restore import change_state
    from check_bundle_restore import fictional_pdf, upload_fixture
    from check_credit_revision_restore import seed_credit_revisions, verify_credit_revisions
    from check_payment_evidence_restore import rows

    s = ledger_setup
    files = DocumentService(s["engine"])
    store = FileStore(tmp_path / "private", 1024 * 1024, 30)
    pdf = fictional_pdf("Fictional credit revision evidence 2026-11-04 USD 20.00")
    evidence = seed_credit_revisions(s["engine"], s["owner"], files, store, pdf, upload_fixture)
    assert len(evidence["groups"]) == 2 and len(evidence["originals"]) == 1
    invoice, bill = evidence["groups"]
    assert invoice["ledger"] != bill["ledger"]
    assert invoice["expected"]["cancelled"] and not bill["expected"]["cancelled"]
    assert invoice["expected"]["facts"] == () and len(bill["expected"]["facts"]) == 2
    assert evidence["originals"][0]["request"].operation_id in {
        operation
        for operation, _version, status, _journal in invoice["expected"]["operations"]
        if status == "cancelled"
    }
    with s["engine"].connect() as connection:
        for case in evidence["groups"]:
            observed = case["expected"]
            assert observed["api_current"]["status"] == (
                "cancelled" if case is invoice else "active"
            )
            assert [row["version"] for row in observed["api_history"]] == [1, 2, 3, 4]
            assert observed["api_revisions"] == tuple(
                item["result"].model_dump(mode="json") for item in case["revisions"]
            )
            assert (
                connection.scalar(select(Entity.archived).where(Entity.id == case["entity"]))
                == case["archived"]
            )
            assert (
                str(
                    connection.scalar(
                        select(LedgerPeriod.closed_through).where(
                            LedgerPeriod.ledger_id == case["ledger"]
                        )
                    )
                )
                == "2026-11-06"
            )
            created = {
                member.operation_id
                for revision in case["revisions"]
                for member in revision["result"].members
                if member.action == "create"
            }
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(BusinessCreditLine)
                    .where(
                        BusinessCreditLine.ledger_id == case["ledger"],
                        BusinessCreditLine.operation_id.in_(created),
                    )
                )
                == 0
            )
            if case is bill:
                assert {
                    status
                    for operation, _version, status, _journal in case["expected"]["operations"]
                    if operation in created
                } == {"active", "cancelled"}
            for item in [case["credit"], *case["revisions"]]:
                saved = connection.execute(
                    select(
                        CommandReceipt.hash_version,
                        CommandReceipt.response_status,
                        CommandReceipt.response,
                    ).where(
                        CommandReceipt.ledger_id == case["ledger"],
                        CommandReceipt.key == item["key"],
                    )
                ).one()
                assert saved.hash_version == 2
                assert saved.response_status == (201 if item is case["credit"] else 200)
                assert saved.response == item["result"].model_dump(mode="json")
            assert len(case["revisions"]) == 3
            # This earlier immutable 200 has different net/tax and dates from the final state.
            middle = case["revisions"][1]["result"]
            assert middle.members[0].net_amount == "18.100000000000000000"
            assert str(middle.credit_date) == "2026-11-05"
    before, changes = rows(s["engine"]), change_state(s["engine"], s["owner"])
    blobs = set((store.root / "blobs").iterdir())
    for _ in range(2):
        verify_credit_revisions(s["engine"], evidence)
        for original in evidence["originals"]:
            metadata, key = files.get_download(
                s["owner"], original["ledger"], original["receipt"].file_id
            )
            with store.open_blob(key, metadata.sha256, metadata.byte_size) as source:
                assert source.read() == pdf
            repeated = upload_fixture(
                files,
                store,
                s["owner"],
                original["ledger"],
                pdf,
                original["request"].original_filename,
                request=original["request"],
            )
            assert repeated["receipt"] == original["receipt"]
        assert rows(s["engine"]) == before
        assert change_state(s["engine"], s["owner"]) == changes
        assert set((store.root / "blobs").iterdir()) == blobs
