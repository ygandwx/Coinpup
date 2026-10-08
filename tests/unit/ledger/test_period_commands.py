"""Period changes retain explicit action/date/version/reason and raw intent identity."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import period_hash
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def payload(**changes):
    return PeriodChange.model_validate(
        dict(
            action="close",
            closed_through="2026-01-31",
            expected_version=1,
            reason="Fictional close",
        )
        | changes
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_version": True},
        {"expected_version": "1"},
        {"expected_version": 2147483647},
        {"reason": " "},
        {"reason": "a\x00b"},
        {"action": "adjust"},
        {"closed_through": "2026-1-1"},
        {"closed_through": 1},
        {"closed_through": "2026-01-31T00:00:00Z"},
    ],
)
def test_explicit_period_inputs(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


def test_null_cutoff_is_explicit_and_raw_reason_spelling_is_retained():
    body = payload(closed_through=None, action="reopen")
    omitted = body.model_dump(exclude={"closed_through"})
    with pytest.raises(ValidationError):
        PeriodChange.model_validate(omitted)
    ledger = uuid4()
    body = payload(reason=" Fictional close ")
    raw = (
        b'{"action":"close","closed_through":"2026-01-31",'
        b'"expected_version":1,"reason":" Fictional close "}'
    )
    assert period_hash(ledger, body, raw) != period_hash(ledger, body)
    assert period_hash(ledger, body) != period_hash(uuid4(), body)
    with pytest.raises(LedgerError):
        period_hash(ledger, body, raw.replace(b"2026-01-31", b"2026-02-01"))
