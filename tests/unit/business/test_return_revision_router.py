"""Invalid wire commands cannot reach the settlement transaction."""

from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService

from tests.unit.business.test_allocation_return_revision_contract import command
from tests.unit.business.test_settlement_router import HEADERS
from tests.unit.ledger.test_ledger_api import LEDGER
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in


@pytest.mark.parametrize("action,suffix", [("correct", "corrections"), ("cancel", "cancellations")])
def test_rejects_unsigned_missing_keys_wrong_versions_and_hidden_replacements(
    signed_in, monkeypatch, action, suffix
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid command reached the financial service")

    monkeypatch.setattr(BusinessAllocationReturnRevisionService, action, forbidden)
    body = command() if action == "correct" else dict(expected_version=1, reason="Fictional cancel")
    path = f"{LEDGER}/business-allocation-returns/{uuid4()}/{suffix}"
    for key in [None, "", "bad key", "x" * 129]:
        headers = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
        if key is not None:
            headers["Idempotency-Key"] = key
        assert signed_in.post(path, json=body, headers=headers).status_code == 422
    values = [body | {"expected_version": 0}, body | {"payment_id": str(uuid4())}]
    if action == "correct":
        line = body["replacement"]["lines"][0]
        for lines in [[], [line] * 201, [line, line], [line | {"payment_amount": 0.1}]]:
            values.append(body | {"replacement": body["replacement"] | {"lines": lines}})
    else:
        values.append(body | {"replacement": None})
    for value in values:
        result = signed_in.post(path, json=value, headers=HEADERS)
        assert result.status_code == 422 and all(
            set(error) == {"loc", "type", "msg"} for error in result.json()["detail"]
        )
    signed_in.cookies.clear()
    denied = signed_in.post(path, json=body, headers=HEADERS)
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
