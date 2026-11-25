from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.device_page import DevicePageReads
from coinpup_api.sync.devices import DeviceRegistry
from coinpup_api.sync.schemas import ChangePage

from tests.integration.sync.test_change_log_database import change_setup as change_setup
from tests.integration.sync.test_device_registry_storage import state

pytestmark = pytest.mark.integration


def test_resume_requires_same_prior_notice_even_when_following_page_matches(change_setup):
    s = change_setup
    engine, owner, device = s["engine"], s["owner"], uuid4()
    registry, reader = DeviceRegistry(engine), DevicePageReads(engine)
    registry.register(owner, device)
    upper = reader.start_cursor(owner).cursor
    first = reader.list_changes(owner, after="0", through=upper, limit=1)
    previous = first.changes[0]
    rest = reader.list_changes(owner, after=first.next_cursor, through=upper, limit=200)
    assert rest.changes
    saved = state(engine)

    def check(*, key=device, who=owner, **updates):
        args = dict(
            prior=previous, after=first.next_cursor, through=upper, limit=200, received=rest
        )
        return reader.check(who, key, **(args | updates))

    assert check(prior=None, after="0", limit=1, received=first) == first.next_cursor
    assert check() == rest.next_cursor
    assert (
        check(
            prior=rest.changes[-1], after=upper, received=ChangePage(changes=[], next_cursor=upper)
        )
        == upper
    )
    for changes, code in [
        ({"prior": None}, "sync_checkpoint_conflict"),
        (
            {"prior": previous.model_copy(update={"entity_id": str(uuid4())})},
            "sync_checkpoint_conflict",
        ),
        ({"prior": previous.model_copy(update={"owner_id": uuid4()})}, "sync_checkpoint_conflict"),
        ({"after": "0", "received": first, "limit": 1}, "sync_checkpoint_conflict"),
        ({"received": rest.model_copy(update={"changes": rest.changes[1:]})}, "sync_page_conflict"),
        ({"key": uuid4()}, "not_found"),
        ({"who": uuid4()}, "not_found"),
    ]:
        with pytest.raises(LedgerError) as err:
            check(**changes)
        assert err.value.code == code
    assert state(engine) == saved
    registry.retire(owner, device)
    terminal = state(engine)
    with pytest.raises(LedgerError) as retired:
        check()
    assert retired.value.code == "sync_device_retired" and state(engine) == terminal
