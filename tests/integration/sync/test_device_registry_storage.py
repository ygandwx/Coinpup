"""Real PostgreSQL owner-bound device evidence, independent from the business change stream."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.sync.device_models import DeviceRevision as Device
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import device_checkpoints_migration

pytestmark = pytest.mark.integration


def state(engine):
    with engine.connect() as db:
        return (
            db.execute(select(Device.__table__).order_by(Device.device_id, Device.version)).all(),
            db.execute(select(ChangeLog.__table__).order_by(ChangeLog.seq)).all(),
        )


def add(engine, row):
    with engine.begin() as db:
        db.execute(insert(Device).values(**row))


def test_distinct_registered_and_retired_devices_never_invent_sync_changes(structure_database):
    engine, owner = structure_database
    old = state(engine)
    first = dict(
        owner_id=owner, device_id=uuid4(), version=1, previous_version=None, state="registered"
    )
    second = first | {"device_id": uuid4()}
    retired = first | {"state": "retired", "version": 2, "previous_version": 1}
    for row in (first, second, retired):
        add(engine, row)
    actual, changes = state(engine)
    assert {(r.device_id, r.state, r.previous_version, r.version) for r in actual} == {
        (r["device_id"], r["state"], r["previous_version"], r["version"])
        for r in [first, second, retired]
    }
    assert all(r.recorded_at.utcoffset() is not None and r.owner_id == owner for r in actual)
    assert changes == old[1]
    for row in [
        first,
        retired,
        retired | {"previous_version": None},
        retired | {"version": 3},
        retired | {"device_id": uuid4()},
        second | {"owner_id": uuid4()},
        first | {"device_id": uuid4(), "state": "retired"},
    ]:
        with pytest.raises(IntegrityError):
            add(engine, row)
    for stmt in [
        update(Device).where(Device.device_id == first["device_id"]).values(device_id=uuid4()),
        delete(Device).where(Device.device_id == second["device_id"]),
    ]:
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as db:
            db.execute(stmt)
    with (
        pytest.raises(IntegrityError, match="prevents downgrade"),
        engine.begin() as db,
        Operations.context(MigrationContext.configure(db)),
    ):
        device_checkpoints_migration()["downgrade"]()
    assert state(engine) == (actual, changes)
