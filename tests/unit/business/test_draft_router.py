"""Draft endpoints preserve session ownership and browser mutation protections."""

from uuid import UUID

import pytest
from coinpup_api.business.drafts import DraftService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.security import csrf_token_for

from tests.unit.ledger.test_ledger_api import LEDGER, ORIGIN, OWNER, RECORD, TOKEN
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

BASE = LEDGER + "/business-documents"
HEADER = dict(
    document_kind="invoice", party_id=RECORD, asset_id="USD", issue_date="2026-10-09", lines=[]
)
WRITES = [
    ("POST", BASE, HEADER | dict(id=RECORD), "create_draft"),
    ("PUT", BASE + "/" + RECORD, HEADER | dict(expected_version=1), "update_draft"),
    (
        "PATCH",
        BASE + "/" + RECORD + "/archive",
        dict(expected_version=1, archived=True),
        "set_draft_archived",
    ),
]


@pytest.mark.parametrize("path", [BASE, BASE + "/" + RECORD])
def test_draft_reads_require_auth_and_no_store(client, path):
    result = client.get(path)
    assert result.status_code == 401 and result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path,body,action", WRITES)
def test_draft_writes_require_origin_csrf_and_session(signed_in, method, path, body, action):
    for origin in (None, "null", "https://attacker.example"):
        headers = {"x-csrf-token": csrf_token_for(TOKEN)}
        if origin is not None:
            headers["origin"] = origin
        result = signed_in.request(method, path, json=body, headers=headers)
        assert result.status_code == 403 and result.json()["detail"] == "origin_not_allowed"
        assert result.headers["cache-control"] == "no-store"
    for csrf in (None, "wrong"):
        headers = {"origin": ORIGIN}
        if csrf is not None:
            headers["x-csrf-token"] = csrf
        result = signed_in.request(method, path, json=body, headers=headers)
        assert result.status_code == 403 and result.json()["detail"] == "csrf_failed"
    signed_in.cookies.clear()
    assert signed_in.request(method, path, json=body, headers={"origin": ORIGIN}).status_code == 401


@pytest.mark.parametrize("method,path,body,action", WRITES)
def test_owned_command_and_safe_domain_error_forwarding(
    signed_in, monkeypatch, method, path, body, action
):
    calls = []

    def capture(self, owner, ledger, *args):
        calls.append((owner, ledger, args))
        raise LedgerError("version_conflict", 409, "Reload before editing.")

    monkeypatch.setattr(DraftService, action, capture)
    result = signed_in.request(
        method, path, json=body, headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)}
    )
    assert result.status_code == 409 and result.json()["detail"]["code"] == "version_conflict"
    assert calls[0][:2] == (OWNER, UUID(RECORD))
    if method != "POST":
        assert calls[0][2][0] == UUID(RECORD)
    assert calls[0][2][-1].model_dump(mode="json", exclude_unset=True) == body


def test_draft_list_is_owned_bounded_and_defaults_explicit(signed_in, monkeypatch):
    calls = []

    def capture(self, owner, ledger, **options):
        calls.append((owner, ledger, options))
        return []

    monkeypatch.setattr(DraftService, "list_drafts", capture)
    assert signed_in.get(BASE).json() == []
    assert calls[-1] == (OWNER, UUID(RECORD), dict(include_archived=True, limit=100, offset=0))
    assert signed_in.get(BASE + "?include_archived=false&limit=2&offset=3").status_code == 200
    assert calls[-1][2] == dict(include_archived=False, limit=2, offset=3)
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(BASE + "?" + query).status_code == 422
    assert len(calls) == 2


@pytest.mark.parametrize(
    "changes",
    [
        dict(total_amount="1.00"),
        dict(issuer_snapshot={}),
        dict(ledger_id=RECORD),
        dict(issue_date=20261009),
    ],
)
def test_creation_rejects_derived_or_ambiguous_fields(signed_in, monkeypatch, changes):
    def unexpected(*args):
        raise AssertionError("Invalid HTTP body reached draft storage")

    monkeypatch.setattr(DraftService, "create_draft", unexpected)
    result = signed_in.post(
        BASE,
        json=HEADER | dict(id=RECORD) | changes,
        headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)},
    )
    assert result.status_code == 422


def test_full_update_cannot_implicitly_clear_missing_lines(signed_in):
    body = {key: value for key, value in HEADER.items() if key != "lines"}
    result = signed_in.put(
        BASE + "/" + RECORD,
        json=body | dict(expected_version=1),
        headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)},
    )
    assert result.status_code == 422


def test_business_types_do_not_rename_the_existing_ocr_contract(client):
    contract = client.app.openapi()
    for path, name in (
        ("/api/v1/ledgers/{ledger_id}/ocr-drafts", "DraftSummary"),
        ("/api/v1/ledgers/{ledger_id}/business-documents", "BusinessDraftSummary"),
    ):
        schema = contract["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert schema["items"]["$ref"] == "#/components/schemas/" + name
    assert "file_id" in contract["components"]["schemas"]["DraftSummary"]["properties"]
    assert (
        "document_kind" in contract["components"]["schemas"]["BusinessDraftSummary"]["properties"]
    )
