"""Control reads retain authentication and bounded typed filters."""

from uuid import UUID

import pytest
from coinpup_api.ledger.control_accounts import ControlAccounts

from tests.unit.ledger.test_posting_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_posting_api import client as client
from tests.unit.ledger.test_posting_api import signed_in as signed_in


def test_control_balances_require_authentication(client):
    assert client.get(LEDGER + "/control-balances").status_code == 401


def test_control_filters_forward_owned_scope(signed_in, monkeypatch):
    seen = []

    def read(self, *args, **kwargs):
        seen.append((args, kwargs))
        return []

    monkeypatch.setattr(ControlAccounts, "balances", read)
    response = signed_in.get(
        LEDGER + "/control-balances",
        params={"system_key": "advance.received", "party_id": RECORD, "limit": 3, "offset": 2},
    )
    assert response.status_code == 200 and response.json() == []
    args, kwargs = seen[0]
    assert args == (OWNER, UUID(RECORD))
    assert kwargs["system_key"] == "advance.received" and kwargs["party_id"] == UUID(RECORD)
    assert kwargs["limit"] == 3 and kwargs["offset"] == 2


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 201},
        {"offset": -1},
        {"party_id": "invalid"},
        {"account_class": "money"},
        {"system_key": "unknown"},
    ],
)
def test_invalid_control_queries_do_not_reach_storage(signed_in, monkeypatch, query):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid query reached storage")

    monkeypatch.setattr(ControlAccounts, "balances", unexpected)
    assert signed_in.get(LEDGER + "/control-balances", params=query).status_code == 422
