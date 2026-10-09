"""Recurring rules retain browser protections and owner-scoped bounded reads."""

from uuid import UUID

import pytest
from coinpup_api.business.recurring_generation import RecurringGenerationService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.security import csrf_token_for

from tests.unit.ledger.test_ledger_api import LEDGER, ORIGIN, OWNER, RECORD, TOKEN
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

BASE = LEDGER + "/recurring-invoice-rules"
BODY = dict(
    id=RECORD,
    name="Fictional monthly",
    timezone_name="Asia/Shanghai",
    anchor_date="2026-01-31",
    frequency="month",
    interval_count=1,
    source_document_id=RECORD,
    source_version=1,
)
WRITES = [
    ("POST", BASE, BODY, "create_rule"),
    (
        "PATCH",
        BASE + "/" + RECORD,
        dict(expected_version=1, name="Fictional renamed"),
        "update_rule",
    ),
    (
        "PATCH",
        BASE + "/" + RECORD + "/archive",
        dict(expected_version=1, archived=True),
        "archive_rule",
    ),
]


@pytest.mark.parametrize(
    "suffix", ["", "/" + RECORD, "/" + RECORD + "/instances", "/" + RECORD + "/instances/0"]
)
def test_reads_require_session_and_disable_caching(client, suffix):
    result = client.get(BASE + suffix)
    assert result.status_code == 401 and result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path,body,action", WRITES)
def test_writes_require_origin_csrf_and_session(signed_in, method, path, body, action):
    for origin in (None, "null", "https://attacker.example"):
        headers = {"x-csrf-token": csrf_token_for(TOKEN)}
        if origin is not None:
            headers["origin"] = origin
        response = signed_in.request(method, path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "origin_not_allowed"
        assert response.headers["cache-control"] == "no-store"
    for csrf in (None, "wrong"):
        headers = {"origin": ORIGIN}
        if csrf is not None:
            headers["x-csrf-token"] = csrf
        response = signed_in.request(method, path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "csrf_failed"
    signed_in.cookies.clear()
    assert signed_in.request(method, path, json=body, headers={"origin": ORIGIN}).status_code == 401


@pytest.mark.parametrize("method,path,body,action", WRITES)
def test_writes_forward_authenticated_owner_and_exact_body(
    signed_in, monkeypatch, method, path, body, action
):
    calls = []

    def capture(self, owner, ledger, *args):
        calls.append((owner, ledger, args))
        raise LedgerError("version_conflict", 409, "Reload before editing.")

    monkeypatch.setattr(RecurringGenerationService, action, capture)
    response = signed_in.request(
        method, path, json=body, headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)}
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "version_conflict"
    assert calls[0][:2] == (OWNER, UUID(RECORD))
    assert calls[0][2][-1].model_dump(mode="json", exclude_unset=True) == body
    if method != "POST":
        assert calls[0][2][0] == UUID(RECORD)


@pytest.mark.parametrize("instances", [False, True])
def test_lists_are_owned_bounded_and_defaults_explicit(signed_in, monkeypatch, instances):
    calls = []

    def capture(self, owner, ledger, *args, **options):
        calls.append((owner, ledger, args, options))
        return []

    monkeypatch.setattr(
        RecurringGenerationService, "list_instances" if instances else "list_rules", capture
    )
    path = BASE + ("/" + RECORD + "/instances" if instances else "")
    defaults = dict(limit=100, offset=0)
    if not instances:
        defaults["include_archived"] = True
    assert signed_in.get(path).json() == []
    assert calls[-1] == (OWNER, UUID(RECORD), (UUID(RECORD),) if instances else (), defaults)
    assert signed_in.get(path + "?limit=2&offset=3").status_code == 200
    assert calls[-1][3] == defaults | dict(limit=2, offset=3)
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(path + "?" + query).status_code == 422
    assert len(calls) == 2


@pytest.mark.parametrize("index", ["-1", "2147483647", "1.5"])
def test_instance_index_is_bounded_before_storage(signed_in, monkeypatch, index):
    def forbidden(*args):
        raise AssertionError("Invalid index reached storage")

    monkeypatch.setattr(RecurringGenerationService, "get_instance", forbidden)
    assert signed_in.get(BASE + "/" + RECORD + "/instances/" + index).status_code == 422


def test_contract_exposes_no_generation_or_mutable_instance_endpoint(client):
    paths = client.app.openapi()["paths"]
    prefix = "/api/v1/ledgers/{ledger_id}/recurring-invoice-rules"
    assert set(path for path in paths if path.startswith(prefix)) == {
        prefix,
        prefix + "/{rule_id}",
        prefix + "/{rule_id}/archive",
        prefix + "/{rule_id}/instances",
        prefix + "/{rule_id}/instances/{index}",
    }
    for suffix in ("/{rule_id}/instances", "/{rule_id}/instances/{index}"):
        assert set(paths[prefix + suffix]) == {"get"}
