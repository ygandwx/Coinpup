"""Credit bundle seed preserves cash, original groups and all change pages on replay."""

from datetime import date
from pathlib import Path

import pytest
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.period_models import LedgerPeriod
from sqlalchemy import select

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_credit_bundle_fixture_replays_every_original_group_without_any_writes(
    ledger_setup, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from check_backup_restore import change_state
    from check_credit_restore import seed_credits, verify_credits
    from check_payment_evidence_restore import rows

    s = ledger_setup
    evidence = seed_credits(s["engine"], s["owner"])
    assert len(evidence["cases"]) == len(evidence["details"]) == 4
    assert len(evidence["confirmations"]) == len(evidence["capacities"]) == 2
    assert {row.asset_id for row in evidence["details"]} == {"USD", "EUR"}
    assert {row.document_kind for row in evidence["details"]} == {"invoice", "bill"}
    assert all(row.lines[0].available_gross_amount == "0.00" for row in evidence["capacities"])
    assert len(evidence["refunds"]["cases"]) == 5
    assert len(evidence["refunds"]["returns"]) == 2
    with s["engine"].connect() as connection:
        assert connection.scalar(
            select(Entity.archived).join(Ledger).where(Ledger.id == evidence["ledger"])
        )
        assert connection.scalar(
            select(LedgerPeriod.closed_through).where(LedgerPeriod.ledger_id == evidence["ledger"])
        ) == date(2026, 10, 5)
    before = rows(s["engine"])
    changes = change_state(s["engine"], s["owner"])
    assert {record["entity_type"] for record in changes[0]} >= {
        "business_credits",
        "business_credit_lines",
        "business_refunds",
        "business_allocation_returns",
    }
    verify_credits(s["engine"], evidence)
    assert rows(s["engine"]) == before
    assert change_state(s["engine"], s["owner"]) == changes
