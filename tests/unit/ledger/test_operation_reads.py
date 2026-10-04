"""Operation query validation must fail before connecting to storage."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.service import LedgerError


@pytest.mark.parametrize(
    "options,code",
    [
        ({"order": "unknown"}, "invalid_order"),
        ({"order": "recognition_date"}, "invalid_order"),
        ({"order": None}, "invalid_order"),
        ({"status": "unknown"}, "invalid_status"),
        ({"limit": 0}, "invalid_pagination"),
        ({"limit": 201}, "invalid_pagination"),
        ({"offset": -1}, "invalid_pagination"),
        ({"offset": 100001}, "invalid_pagination"),
        (
            {"from_date": date(2026, 1, 3), "to_date": date(2026, 1, 2)},
            "invalid_date_range",
        ),
    ],
)
def test_operation_query_rejects_invalid_filters_before_storage(options, code):
    with pytest.raises(LedgerError) as error:
        PostingService(None).list_operations(uuid4(), uuid4(), **options)
    assert (error.value.code, error.value.status) == (code, 422)


@pytest.mark.parametrize("parameter", ["from_date", "to_date"])
@pytest.mark.parametrize(
    "invalid_date",
    [0, True, "2026-02-30", "20260102", "2026-01-02T00:00:00Z", datetime(2026, 1, 2, tzinfo=UTC)],
)
def test_operation_query_rejects_non_calendar_bounds_before_storage(parameter, invalid_date):
    with pytest.raises(LedgerError) as error:
        PostingService(None).list_operations(uuid4(), uuid4(), **{parameter: invalid_date})
    assert (error.value.code, error.value.status) == ("invalid_date", 422)
