from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.devices import DeviceRegistry
from coinpup_api.sync.service import ChangeService

from tests.integration.sync.test_device_registry_storage import state

pytestmark = pytest.mark.integration


def race(action):
    barrier = Barrier(2)

    def call():
        barrier.wait(5)
        return action()

    with ThreadPoolExecutor(max_workers=2) as workers:
        a, b = [workers.submit(call) for _ in range(2)]
        left, right = a.result(10), b.result(10)
    assert left == right
    return left


@pytest.mark.slow
def test_concurrent_registration_retirement_and_old_register_replay(structure_database):
    engine, owner = structure_database
    registry, stream = DeviceRegistry(engine), ChangeService(engine)
    device, another = uuid4(), uuid4()
    cursor = stream.start_cursor(owner)
    initial = race(lambda: registry.register(owner, device))
    assert initial.version == 1 and initial.state == "registered"
    other = registry.register(owner, another)
    terminal = race(lambda: registry.retire(owner, device))
    assert terminal.state == "retired" and terminal.version == 2
    before = state(engine)
    assert registry.register(owner, device) == registry.retire(owner, device) == terminal
    assert registry.history(owner, device) == (initial, terminal)
    assert registry.history(owner, another) == (other,)
    assert registry.register(owner, another) == other and other.device_id != device
    assert state(engine) == before and len(before[0]) == 3
    assert stream.start_cursor(owner) == cursor


def test_unknown_identity_does_not_disclose_or_create_device_state(structure_database):
    engine, owner = structure_database
    registry = DeviceRegistry(engine)
    device = uuid4()
    registry.register(owner, device)
    before = state(engine)
    for fn, who, key, expected in [
        (registry.history, uuid4(), device, "not_found"),
        (registry.register, uuid4(), device, "not_found"),
        (registry.retire, uuid4(), device, "not_found"),
        (registry.retire, owner, uuid4(), "not_found"),
        (registry.history, owner, uuid4(), "not_found"),
        (registry.register, owner, str(device), "invalid_device"),
    ]:
        with pytest.raises(LedgerError) as err:
            fn(who, key)
        assert err.value.code == expected
    assert state(engine) == before
