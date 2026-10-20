"""Actual refund bundle seed verifies closed/archived original receipts without any writes."""

from datetime import date
from pathlib import Path

import pytest
from coinpup_api.ledger.models import Account, Entity, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from sqlalchemy import select

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_refund_bundle_fixture_preserves_all_tables_and_change_pages(ledger_setup, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_backup_restore import change_state
    from check_payment_evidence_restore import rows
    from check_refund_restore import seed_refunds, verify_refunds

    s = ledger_setup
    evidence = seed_refunds(s["engine"], s["owner"])
    assert len(evidence["returns"]) == 2
    assert len(evidence["cases"]) == len(evidence["details"]) == 5
    assert {row.asset_id for row in evidence["details"]} == {"USD", "EUR"}
    assert len(evidence["capacities"]) == 3
    with s["engine"].connect() as connection:
        account_id = evidence["cases"][0]["payload"].account_id
        assert connection.scalar(select(Account.archived).where(Account.id == account_id))
        assert connection.scalar(
            select(Entity.archived).join(Ledger).where(Ledger.id == evidence["ledger"])
        )
        assert connection.scalar(
            select(LedgerPeriod.closed_through).where(LedgerPeriod.ledger_id == evidence["ledger"])
        ) == date(2026, 10, 4)
    before = rows(s["engine"])
    changes = change_state(s["engine"], s["owner"])
    assert {record["entity_type"] for record in changes[0]} >= {
        "business_refunds",
        "business_refund_returns",
    }
    verify_refunds(s["engine"], evidence)
    assert rows(s["engine"]) == before
    assert change_state(s["engine"], s["owner"]) == changes
