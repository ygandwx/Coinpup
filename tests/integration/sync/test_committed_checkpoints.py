"""Real immutable changes; checking a received page neither writes progress nor posts anything."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import CategoryCreate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.schemas import ChangePage
from coinpup_api.sync.service import ChangeService

from tests.integration.sync.test_change_log_database import change_setup as change_setup
from tests.integration.sync.test_change_log_database import log_rows

pytestmark = pytest.mark.integration


def test_snapshot_validates_the_actual_prefix_and_not_just_its_tail(change_setup):
    s = change_setup
    owner = s["owner"]
    reader = ChangeService(s["engine"])
    start = reader.start_cursor(owner).cursor

    def category(name):
        return s["structure"].create_category(
            owner, s["ledger"], CategoryCreate(name=name, kind="expense")
        )

    for i in range(3):
        category(f"Fictional checkpoint {i}")
    upper = reader.start_cursor(owner).cursor
    first = reader.list_changes(owner, after=start, through=upper, limit=2)
    rest = reader.list_changes(owner, after=first.next_cursor, through=upper, limit=2)
    assert len(first.changes) == 2 and len(rest.changes) == 1
    category("Fictional later write")
    before = log_rows(s["engine"])

    def check(received, after=start, who=owner):
        return reader.verify_received_page(
            who, after=after, through=upper, limit=2, received=received
        )

    assert check(first) == check(first) == first.next_cursor
    assert check(rest, first.next_cursor) == upper
    empty = ChangePage(changes=[], next_cursor=upper)
    assert check(empty, upper) == upper
    swapped = first.changes[0].model_copy(update={"entity_id": str(uuid4())})
    for wrong, kwargs in [
        (first.model_copy(update={"changes": [swapped, first.changes[1]]}), {}),
        (first.model_copy(update={"changes": first.changes[1:]}), {}),
        (rest, {}),
        (first, {"who": uuid4()}),
    ]:
        with pytest.raises(LedgerError) as blocked:
            check(wrong, **kwargs)
        assert blocked.value.status in (404, 409)
        if "who" not in kwargs:
            assert blocked.value.code == "sync_page_conflict"
    assert log_rows(s["engine"]) == before
