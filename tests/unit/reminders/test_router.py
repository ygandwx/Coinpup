"""Reminder HTTP routes preserve session/CSRF boundaries and explicit action bodies."""

from uuid import UUID

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.reminders.service import ReminderService
from coinpup_api.security import csrf_token_for

from tests.unit.ledger.test_ledger_api import LEDGER, ORIGIN, OWNER, RECORD, TOKEN
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

BASE = LEDGER + "/reminders"
BODY = dict(id=RECORD, title="Fictional deadline", event_kind="tax")
RULE = dict(
    rule_id="certificate.expiry",
    rule_version="2026-10-10.1",
    expiry_date="2027-01-31",
    applicability_confirmed=True,
)
WRITES = [
    ("POST", BASE, BODY, "create_event"),
    (
        "PATCH",
        BASE + "/" + RECORD,
        dict(expected_version=1, title="Fictional renamed"),
        "edit_event",
    ),
    (
        "POST",
        BASE + "/" + RECORD + "/recalculate",
        dict(expected_version=1, rule=RULE),
        "recalculate_event",
    ),
    (
        "PATCH",
        BASE + "/" + RECORD + "/manual-date",
        dict(expected_version=1, manual_due_date="2027-02-01", reason="Fictional notice"),
        "set_manual_date",
    ),
    (
        "PATCH",
        BASE + "/" + RECORD + "/manual-date",
        dict(expected_version=1, manual_due_date=None, reason="Fictional withdrawal"),
        "set_manual_date",
    ),
    (
        "POST",
        BASE + "/" + RECORD + "/transition",
        dict(expected_version=1, action="complete"),
        "transition_event",
    ),
]


@pytest.mark.parametrize("suffix", ["", "/rules", "/" + RECORD, "/" + RECORD + "/revisions"])
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
def test_commands_forward_authenticated_owner_and_exact_body(
    signed_in, monkeypatch, method, path, body, action
):
    calls = []

    def capture(self, owner, ledger, *args):
        calls.append((owner, ledger, args))
        raise LedgerError("version_conflict", 409, "Reload before editing.")

    monkeypatch.setattr(ReminderService, action, capture)
    response = signed_in.request(
        method, path, json=body, headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)}
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "version_conflict"
    assert calls[0][:2] == (OWNER, UUID(RECORD))
    assert calls[0][2][-1].model_dump(mode="json", exclude_unset=True) == body
    if method != "POST" or path != BASE:
        assert calls[0][2][0] == UUID(RECORD)


@pytest.mark.parametrize("history", [False, True])
def test_lists_have_bounded_pagination_and_owned_defaults(signed_in, monkeypatch, history):
    calls = []

    def capture(self, owner, ledger, *args, **options):
        calls.append((owner, ledger, args, options))
        return []

    monkeypatch.setattr(ReminderService, "list_revisions" if history else "list_events", capture)
    path = BASE + ("/" + RECORD + "/revisions" if history else "")
    assert signed_in.get(path).json() == []
    expected = dict(limit=100, offset=0)
    if not history:
        expected.update(include_archived=True, include_completed=True)
    assert calls[0] == (OWNER, UUID(RECORD), (UUID(RECORD),) if history else (), expected)
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(path + "?" + query).status_code == 422
    assert len(calls) == 1


def test_reminder_contract_has_no_delete_or_financial_command(client):
    paths = client.app.openapi()["paths"]
    prefix = "/api/v1/ledgers/{ledger_id}/reminders"
    assert set(path for path in paths if path.startswith(prefix)) == {
        prefix,
        prefix + "/rules",
        prefix + "/{event_id}",
        prefix + "/{event_id}/manual-date",
        prefix + "/{event_id}/recalculate",
        prefix + "/{event_id}/transition",
        prefix + "/{event_id}/revisions",
    }
    assert all("delete" not in paths[path] for path in paths if path.startswith(prefix))
