"""The database seals each nonempty committed owner prefix and its unique device predecessor."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.sync.ack_models import PageAcknowledgement as Ack
from coinpup_api.sync.devices import DeviceRegistry
from coinpup_api.sync.models import ChangeLog
from coinpup_api.sync.schemas import ChangeStart
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import device_checkpoints_migration
from tests.integration.sync.test_change_log_database import change_setup as change_setup

pytestmark = pytest.mark.integration


def put(db, v):
    db.execute(insert(Ack).values(**v))


def test_chain_can_neither_fork_skip_nonempty_notifications_nor_advance_a_retired_device(
    change_setup,
):
    s = change_setup
    engine, owner = s["engine"], s["owner"]
    registry = DeviceRegistry(engine)
    device, peer = uuid4(), uuid4()
    registry.register(owner, device)
    with engine.connect() as db:
        seqs = db.scalars(
            select(ChangeLog.seq).where(ChangeLog.owner_id == owner).order_by(ChangeLog.seq)
        ).all()
    assert len(seqs) >= 5
    position = str(seqs[0])
    registry.register(
        owner,
        peer,
        ChangeStart(cursor=position, witness=ChangeService(engine).change_at(owner, position)),
    )
    root = dict(
        owner_id=owner,
        device_id=device,
        after_seq=0,
        through_seq=seqs[-1],
        page_limit=1,
        row_count=1,
        last_seq=seqs[0],
        prior_tail=None,
    )
    with engine.begin() as db:
        put(db, root)
    child = root | dict(
        after_seq=seqs[0], prior_tail=seqs[0], last_seq=seqs[2], page_limit=2, row_count=2
    )
    for bad in [
        child | {"device_id": peer},
        child | {"owner_id": uuid4()},
        child | {"device_id": uuid4()},
        child | {"last_seq": seqs[3]},
        child | {"row_count": 1},
        child | {"prior_tail": None},
        child | {"through_seq": seqs[-1] + 20},
        child | {"page_limit": 201},
        child | {"after_seq": seqs[1], "prior_tail": seqs[0], "last_seq": seqs[3]},
    ]:
        with pytest.raises(IntegrityError), engine.begin() as db:
            put(db, bad)
    with engine.begin() as db:
        put(db, child)
        # Peer starts after its full snapshot at a nonzero real owner anchor.
        put(db, child | {"device_id": peer, "prior_tail": None})
    for stmt in [update(Ack).values(page_limit=3), delete(Ack)]:
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as db:
            db.execute(stmt)
    registry.retire(owner, device)
    with pytest.raises(IntegrityError), engine.begin() as db:
        put(db, root | dict(after_seq=seqs[2], prior_tail=seqs[2], last_seq=seqs[3]))
    with (
        pytest.raises(IntegrityError, match="prevents downgrade"),
        engine.begin() as db,
        Operations.context(MigrationContext.configure(db)),
    ):
        device_checkpoints_migration()["downgrade"]()
    with engine.connect() as db:
        assert len(db.execute(select(Ack.__table__)).all()) == 3
        assert (
            db.scalars(
                select(ChangeLog.seq).where(ChangeLog.owner_id == owner).order_by(ChangeLog.seq)
            ).all()
            == seqs
        )
