"""The full return recovery fixture replays after archive and close without any writes."""

from pathlib import Path

import pytest

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_return_bundle_fixture_preserves_all_tables_on_replay(ledger_setup, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_allocation_return_restore import seed_allocation_returns, verify_allocation_returns
    from check_backup_restore import change_state
    from check_payment_evidence_restore import rows

    s = ledger_setup
    evidence = seed_allocation_returns(s["engine"], s["owner"])
    assert len(evidence["cases"]) == len(evidence["details"]) == 4
    assert len(evidence["reapplied"]) == 2
    assert {
        line.document_asset_id for capacity in evidence["capacities"] for line in capacity.lines
    } == {"USD", "EUR"}
    before = rows(s["engine"])
    changes = change_state(s["engine"], s["owner"])
    assert {record["entity_type"] for record in changes[0]} >= {
        "business_allocation_returns",
        "business_allocation_return_lines",
    }
    verify_allocation_returns(s["engine"], evidence)
    assert rows(s["engine"]) == before
    assert change_state(s["engine"], s["owner"]) == changes
