"""Invalid scheduler inputs fail before opening any database transaction."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from coinpup_api.business.recurring_generation import RecurringGenerationService, aware_instant
from coinpup_api.ledger.service import LedgerError


@pytest.mark.parametrize("instant", [None, "2026-01-31T00:00:00Z", datetime(2026, 1, 31)])
def test_generation_requires_an_explicit_aware_instant(instant):
    with pytest.raises(LedgerError) as error:
        RecurringGenerationService(None).generate_occurrence(
            uuid4(), uuid4(), uuid4(), 0, now=instant
        )
    assert error.value.code == "recurrence_instant" and error.value.status == 422


@pytest.mark.parametrize("index", [-1, True, "0", 0.5, 2147483647])
def test_generation_requires_a_supported_integer_index(index):
    with pytest.raises(LedgerError) as error:
        RecurringGenerationService(None).generate_occurrence(
            uuid4(), uuid4(), uuid4(), index, now=datetime(2026, 1, 31, tzinfo=UTC)
        )
    assert error.value.code == "recurrence_index"
    with pytest.raises(LedgerError) as error:
        RecurringGenerationService(None).get_instance(uuid4(), uuid4(), uuid4(), index)
    assert error.value.code == "recurrence_index"


def test_aware_offset_is_preserved_without_using_the_host_timezone():
    instant = datetime(2026, 1, 31, tzinfo=timezone(timedelta(hours=8)))
    assert aware_instant(instant) is instant
