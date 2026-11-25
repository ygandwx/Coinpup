from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.ack_models import PageAcknowledgement as Ack
from coinpup_api.sync.acknowledgements import DeviceAcknowledgements
from coinpup_api.sync.devices import DeviceRegistry
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.schemas import ChangeStart
from sqlalchemy import select

from tests.integration.sync.test_change_log_database import change_setup as change_setup
from tests.integration.sync.test_device_registry_service import race

pytestmark = pytest.mark.integration


@pytest.mark.slow
def test_exact_historical_replay_stays_separate_from_current_progress(change_setup):
    s = change_setup
    engine, owner = s["engine"], s["owner"]
    registry = DeviceRegistry(engine)
    svc = DeviceAcknowledgements(engine)
    a, b = uuid4(), uuid4()
    registry.register(owner, a)
    upper = svc.start_cursor(owner).cursor

    def page(prior, limit):
        after = prior.seq if prior else "0"
        value = svc.list_changes(owner, after=after, through=upper, limit=limit)
        return dict(prior=prior, after=after, through=upper, limit=limit, received=value)

    def snapshot():
        with engine.connect() as db:
            return (
                db.execute(select(Ack.__table__).order_by(Ack.device_id, Ack.last_seq)).all(),
                db.execute(select(ChangeLog.__table__).order_by(ChangeLog.seq)).all(),
            )

    before = snapshot()
    first = page(None, 2)
    second = page(first["received"].changes[-1], 1)
    registry.register(owner, b, ChangeStart(cursor=second["after"], witness=second["prior"]))
    assert svc.latest(owner, a) is None
    original = race(lambda: svc.acknowledge(owner, a, **first))
    next_page = svc.acknowledge(owner, a, **second)
    assert (
        original.cursor == first["received"].next_cursor
        and next_page.cursor == second["received"].next_cursor
    )
    assert svc.latest(owner, a) == next_page and svc.latest(owner, b) is None
    peer = svc.acknowledge(
        owner, b, **second
    )  # An independently bootstrapped device, positive anchor.
    assert peer.device_id == b and peer.after == original.cursor
    stable = snapshot()
    assert len(stable[0]) == 3 and stable[1] == before[1]
    assert svc.acknowledge(owner, a, **first) == original and svc.latest(owner, a) == next_page
    changed = first["received"].changes[0].model_copy(update={"entity_id": str(uuid4())})
    for bad, code in [
        (first | {"limit": 1}, "sync_checkpoint_conflict"),
        (
            first
            | {
                "received": first["received"].model_copy(
                    update={"changes": [changed, first["received"].changes[-1]]}
                )
            },
            "sync_page_conflict",
        ),
        (second | {"prior": changed}, "sync_checkpoint_conflict"),
        (page(svc.change_at(owner, upper), 1), "sync_empty_page"),
    ]:
        with pytest.raises(LedgerError) as err:
            svc.acknowledge(owner, a, **bad)
        assert err.value.code == code
    registry.retire(owner, a)
    assert svc.acknowledge(owner, a, **first) == original and svc.latest(owner, a) == next_page
    with pytest.raises(LedgerError) as err:
        svc.acknowledge(owner, a, **page(second["received"].changes[-1], 1))
    assert err.value.code == "sync_device_retired"
    with pytest.raises(LedgerError) as foreign:
        svc.acknowledge(uuid4(), a, **first)
    assert foreign.value.code == "not_found"
    assert snapshot() == stable
