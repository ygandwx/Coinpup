"""Read-only closing ranges retain relevant reopen metadata after reclosure."""

from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.ledger.period_reads import PeriodReads
from coinpup_api.ledger.service import LedgerError

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_logical_open_and_closed_partial_ranges_do_not_write(ledger_setup):
    s = ledger_setup
    service = PeriodReads(s["engine"])
    before = snapshot(s)
    initial = service.state(s["owner"], s["ledger"])
    assert initial.version == 1 and initial.closed_through is None
    assert initial.range_status is None and initial.last_reopened is None
    assert service.history(s["owner"], s["ledger"]) == [] and snapshot(s) == before
    change(s)
    reopened = change(s, 2, "2026-01-15", "reopen")
    change(s, 3)
    before = snapshot(s)
    for start, end, expected, relevant in [
        ("2026-01-01", "2026-01-31", "closed", True),
        ("2026-01-01", "2026-02-01", "partial", True),
        ("2026-02-01", "2026-02-02", "open", False),
        ("2026-01-01", "2026-01-15", "closed", False),
    ]:
        for basis in ("transaction", "recognition"):
            result = service.state(
                s["owner"],
                s["ledger"],
                from_date=date.fromisoformat(start),
                to_date=date.fromisoformat(end),
                date_basis=basis,
            )
            assert (
                result.version == 4
                and result.range_status == expected
                and result.date_basis == basis
            )
            assert result.last_reopened == (reopened if relevant else None)
            assert result.generated_at.tzinfo is not None
    assert [row.version for row in service.history(s["owner"], s["ledger"], limit=1, offset=1)] == [
        3
    ]
    assert snapshot(s) == before
    with pytest.raises(LedgerError) as failure:
        service.state(s["owner"], uuid4())
    assert failure.value.code == "not_found"


@pytest.mark.parametrize(
    "filters",
    [
        {"from_date": date(2026, 1, 1)},
        {"from_date": date(2026, 2, 1), "to_date": date(2026, 1, 1)},
        {"date_basis": "unknown"},
    ],
)
def test_invalid_ranges_are_rejected(ledger_setup, filters):
    s = ledger_setup
    with pytest.raises(LedgerError) as failure:
        PeriodReads(s["engine"]).state(s["owner"], s["ledger"], **filters)
    assert failure.value.code == "invalid_date_range"


def test_real_http_changes_replay_and_audit_read(ledger_setup, authenticated_client):
    s = ledger_setup
    client, _, _ = authenticated_client
    base = f"/api/v1/ledgers/{s['ledger']}"
    body = {
        "action": "close",
        "closed_through": "2026-01-31",
        "expected_version": 1,
        "reason": "Fictional HTTP closing",
    }
    headers = {"Idempotency-Key": "fictional-period-http"}
    first = client.post(base + "/period-changes", json=body, headers=headers)
    assert first.status_code == 201
    before = snapshot(s)
    assert client.post(base + "/period-changes", json=body, headers=headers).json() == first.json()
    assert client.get(base + "/period-changes").json() == [first.json()]
    assert client.get(base + "/period").json()["version"] == 2 and snapshot(s) == before
