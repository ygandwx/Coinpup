"""Business transactions acquire the cursor lock before owner or business access."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerService
from coinpup_api.models import Administrator


@pytest.mark.parametrize(
    "options,prefix",
    [
        ({}, []),
        ({"write": True}, [("SELECT pg_advisory_xact_lock(:key)", {"key": 18945999704708432})]),
        (
            {"read_only": True},
            [("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", None)],
        ),
    ],
)
def test_transaction_lock_order_preserves_default_and_read_only_transactions(
    monkeypatch, options, prefix
):
    owner = uuid4()
    actions = []

    class Session:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin(self):
            return nullcontext()

        def execute(self, statement, parameters=None):
            actions.append((str(statement), parameters))

        def get(self, model, identifier):
            assert model is Administrator and identifier == owner
            actions.append("owner lookup")
            return object()

    monkeypatch.setattr("coinpup_api.ledger.service.Session", Session)
    with LedgerService(object())._transaction(owner, **options):
        actions.append("business access")
    assert actions == [*prefix, "owner lookup", "business access"]
