"""Real HTTP session/configuration lifecycle; does not issue a number or create PDFs."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService

from tests.integration.ledger.test_account_classes import snapshot

pytestmark = pytest.mark.integration


@pytest.fixture
def profiles(authenticated_client):
    client, engine, owner = authenticated_client
    service = LedgerService(engine)
    companies = [
        service.create_entity(
            owner,
            EntityCreate(
                kind="company",
                name=f"Fictional issuer {n}",
                country_code="HK",
                company_type="private_limited",
                base_asset_id="USD",
            ),
        ).ledger.id
        for n in range(2)
    ]
    personal = service.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional personal", base_asset_id="USD")
    ).ledger.id
    body = dict(
        id=str(uuid4()),
        name="Fictional 开票配置",
        numbering={"prefix": "虚构-", "minimum_digits": 6},
        footer="Fictional footer",
    )
    return dict(
        client=client,
        engine=engine,
        owner=owner,
        ledger=companies[0],
        other=companies[1],
        personal=personal,
        base=f"/api/v1/ledgers/{companies[0]}/invoice-profiles",
        body=body,
    )


def test_full_http_master_data_lifecycle_keeps_scope_version_and_no_store(profiles):
    s = profiles
    client, base, body = s["client"], s["base"], s["body"]
    before = snapshot(s)
    created = client.post(base, json=body)
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    original = created.json()
    assert original["id"] == body["id"] and original["ledger_id"] == str(s["ledger"])
    assert original["numbering"] == body["numbering"] and original["version"] == 1
    assert not {"sequence", "number", "artifacts", "issuance_id"} & original.keys()
    path = base + "/" + body["id"]
    duplicate = client.post(base, json=body)
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "duplicate_record"
    assert client.get(path).json() == original and client.get(base).json() == [original]
    changed = client.put(
        path,
        json=dict(
            expected_version=1,
            name="Fictional new name",
            numbering={"prefix": "F-", "minimum_digits": 20},
        ),
    )
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert changed.json()["footer"] is None
    assert (
        client.put(
            path, json=dict(expected_version=1, name="Stale", numbering=body["numbering"])
        ).status_code
        == 409
    )
    archived = client.patch(path + "/archive", json=dict(expected_version=2, archived=True))
    assert (
        archived.status_code == 200
        and archived.json()["archived"]
        and archived.json()["version"] == 3
    )
    assert client.get(base + "?include_archived=false").json() == []
    restored = client.patch(path + "/archive", json=dict(expected_version=3, archived=False))
    assert restored.status_code == 200 and restored.json()["version"] == 4
    saved = snapshot(s)
    for endpoint in [base, path]:
        response = client.get(endpoint)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert snapshot(s) == saved
    ignored = {"business_invoice_profiles", "change_log"}
    assert {k: v for k, v in before.items() if k not in ignored} == {
        k: v for k, v in saved.items() if k not in ignored
    }


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
@pytest.mark.parametrize("fault", ["origin", "csrf", "session"])
def test_every_write_has_real_session_origin_and_csrf_boundaries(profiles, method, fault):
    s = profiles
    client, base = s["client"], s["base"]
    assert client.post(base, json=s["body"]).status_code == 201
    path = (
        base
        if method == "POST"
        else base + "/" + s["body"]["id"] + ("/archive" if method == "PATCH" else "")
    )
    body = (
        s["body"]
        if method == "POST"
        else dict(expected_version=1, archived=True)
        if method == "PATCH"
        else {k: v for k, v in s["body"].items() if k != "id"} | dict(expected_version=1)
    )
    if fault == "session":
        client.cookies.clear()
    else:
        client.headers["origin" if fault == "origin" else "x-csrf-token"] = "fictional-invalid"
    before = snapshot(s)
    response = client.request(method, path, json=body)
    assert response.status_code == (401 if fault == "session" else 403)
    assert response.headers["cache-control"] == "no-store" and snapshot(s) == before


@pytest.mark.parametrize("suffix", ["", "/id"])
def test_reads_require_real_session(profiles, suffix):
    s = profiles
    s["client"].cookies.clear()
    response = s["client"].get(s["base"] + suffix.replace("id", s["body"]["id"]))
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_company_ledger_isolation_and_closed_record_identity(profiles):
    s = profiles
    client = s["client"]
    assert client.post(s["base"], json=s["body"]).status_code == 201
    other = f"/api/v1/ledgers/{s['other']}/invoice-profiles"
    before = snapshot(s)
    assert client.get(other).json() == []
    assert client.get(other + "/" + s["body"]["id"]).status_code == 404
    assert (
        client.put(
            other + "/" + s["body"]["id"],
            json=dict(expected_version=1, name="Wrong company", numbering=s["body"]["numbering"]),
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/v1/ledgers/{s['personal']}/invoice-profiles", json=s["body"]).status_code
        == 422
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "patch",
    [
        {"numbering": {"prefix": "F-", "minimum_digits": True}},
        {"logo_file_id": str(uuid4())},
        {"next_sequence": "1"},
        {"number": "F-000001"},
        {"owner_id": str(uuid4())},
        {"footer": "bad\u0000input"},
        {"numbering": {"prefix": " bad", "minimum_digits": 6}},
    ],
)
def test_configuration_cannot_supply_numbers_or_bypass_bounded_review_fields(profiles, patch):
    s = profiles
    before = snapshot(s)
    response = s["client"].post(s["base"], json=s["body"] | patch)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert snapshot(s) == before


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "offset=100001"])
def test_invalid_http_pages_are_rejected_before_reading_configuration(profiles, query):
    s = profiles
    before = snapshot(s)
    assert s["client"].get(s["base"] + "?" + query).status_code == 422
    assert snapshot(s) == before
