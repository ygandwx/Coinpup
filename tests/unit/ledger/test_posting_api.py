"""Financial HTTP boundaries must preserve exact commands and session ownership."""

import json
from datetime import date
from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.main import create_app
from coinpup_api.security import csrf_token_for
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

ORIGIN = "http://localhost:8000"
TOKEN = "fictional-posting-unit-session"
OWNER = UUID("00000000-0000-0000-0000-000000000001")
RECORD = "00000000-0000-0000-0000-000000000002"
LEDGER = f"/api/v1/ledgers/{RECORD}"
BASE = {
    "account_id": RECORD,
    "asset_id": "USD",
    "amount": "10.00",
    "transaction_date": "2026-10-03",
}
CLASSIFIED = BASE | {
    "recognition_date": "2026-09-30",
    "splits": [{"category_id": RECORD, "amount": "10.00"}],
}
TRANSFER = {
    "source_account_id": RECORD,
    "destination_account_id": "00000000-0000-0000-0000-000000000003",
    "asset_id": "USD",
    "amount": "10.00",
    "transaction_date": "2026-10-03",
}
EXCHANGE = {
    "source_account_id": RECORD,
    "destination_account_id": "00000000-0000-0000-0000-000000000003",
    "source_asset_id": "USD",
    "source_amount": "10.00",
    "destination_asset_id": "EUR",
    "destination_amount": "9.00",
    "transaction_date": "2026-10-03",
}
WRITES = [
    ("/opening-balances", BASE, "post_opening"),
    ("/income", CLASSIFIED, "post_income"),
    ("/expenses", CLASSIFIED, "post_expense"),
    ("/transfers", TRANSFER, "post_transfer"),
    ("/exchanges", EXCHANGE, "post_exchange"),
]
CORRECTION = {
    "expected_version": 1,
    "reason": "Fictional corrected amount",
    "replacement": {"kind": "expense", **CLASSIFIED},
}
CANCELLATION = {"expected_version": 1, "reason": "Fictional cancelled entry"}
REVISIONS = [
    (f"/operations/{RECORD}/corrections", CORRECTION, "correct_operation"),
    (f"/operations/{RECORD}/cancellations", CANCELLATION, "cancel_operation"),
]


class Probe:
    def check(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    with TestClient(create_app(Settings(_env_file=None, environment="test"), Probe())) as client:
        yield client


@pytest.fixture
def signed_in(client, monkeypatch):
    def resolve(self, token):
        if token != TOKEN:
            raise AuthError("authentication_required", 401)
        return Identity(OWNER, "fictional-admin")

    monkeypatch.setattr(AuthService, "get_session", resolve)
    client.cookies.set("coinpup_session", TOKEN)
    client.headers.update(
        {"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN), "idempotency-key": "test-command"}
    )
    return client


@pytest.mark.parametrize(
    "suffix", ["/operations", f"/operations/{RECORD}", f"/operations/{RECORD}/history", "/balances"]
)
def test_financial_reads_require_session(client, suffix):
    response = client.get(LEDGER + suffix)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("suffix,body,method", WRITES + REVISIONS)
def test_financial_writes_guard_origin_session_and_csrf(
    signed_in, suffix, body, method, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized financial service call")

    monkeypatch.setattr(PostingService, method, forbidden)
    for origin in ("null", "https://attacker.example"):
        assert (
            signed_in.post(LEDGER + suffix, json=body, headers={"origin": origin}).status_code
            == 403
        )
    signed_in.headers.pop("origin")
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 403
    signed_in.headers["origin"] = ORIGIN
    signed_in.headers.pop("x-csrf-token")
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 403
    signed_in.headers["x-csrf-token"] = "wrong"
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 403
    signed_in.cookies.clear()
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 401


@pytest.mark.parametrize("suffix,body,method", WRITES)
def test_financial_command_forwards_exact_body_owner_and_key(
    signed_in, suffix, body, method, monkeypatch
):
    calls = []

    def record(self, owner_id, ledger_id, payload, key, *, raw_body):
        calls.append((owner_id, ledger_id, payload.model_dump(mode="json"), key, raw_body))
        raise LedgerError("synthetic_conflict", 409, "Synthetic conflict")

    monkeypatch.setattr(PostingService, method, record)
    response = signed_in.post(LEDGER + suffix, json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "synthetic_conflict"
    assert len(calls) == 1
    owner_id, ledger_id, payload, key, raw = calls[0]
    assert (owner_id, ledger_id, key) == (OWNER, UUID(RECORD), "test-command")
    assert isinstance(raw, bytes) and json.loads(raw) == body
    assert "id" not in json.loads(raw) and payload["id"] is None
    assert "description" not in json.loads(raw) and payload["description"] == ""
    if suffix == "/exchanges":
        assert payload["source_amount"] == "10.00"
        assert payload["destination_amount"] == "9.00"
    else:
        assert payload["amount"] == "10.00"
    assert payload["transaction_date"] == "2026-10-03"
    if suffix in {"/income", "/expenses"}:
        assert payload["recognition_date"] == "2026-09-30"
        assert payload["splits"] == body["splits"]


@pytest.mark.parametrize("suffix,body,method", REVISIONS)
def test_revision_forwards_path_identity_version_and_key(
    signed_in, suffix, body, method, monkeypatch
):
    calls = []

    def record(self, owner_id, ledger_id, operation_id, payload, key, *, raw_body):
        calls.append(
            (owner_id, ledger_id, operation_id, payload.model_dump(mode="json"), key, raw_body)
        )
        raise LedgerError("version_conflict", 409, "The version changed")

    monkeypatch.setattr(PostingService, method, record)
    response = signed_in.post(LEDGER + suffix, json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "version_conflict"
    owner, ledger, operation, payload, key, raw = calls[0]
    assert (owner, ledger, operation, key) == (OWNER, UUID(RECORD), UUID(RECORD), "test-command")
    assert isinstance(raw, bytes) and json.loads(raw) == body
    assert payload["expected_version"] == 1 and payload["reason"] == body["reason"]
    if method == "correct_operation":
        assert payload["replacement"]["amount"] == "10.00"
        assert "id" not in payload["replacement"]


@pytest.mark.parametrize("suffix,body,method", WRITES + REVISIONS)
def test_financial_dependency_forwards_actual_cached_request_bytes(
    signed_in, suffix, body, method, monkeypatch
):
    calls = []
    received = json.dumps(body, ensure_ascii=True, indent=3).encode("utf-8")

    def record(*args, raw_body):
        calls.append(raw_body)
        raise LedgerError("synthetic_conflict", 409, "Synthetic conflict")

    monkeypatch.setattr(PostingService, method, record)
    response = signed_in.post(
        LEDGER + suffix, content=received, headers={"content-type": "application/json"}
    )
    assert response.status_code == 409
    assert calls == [received]


@pytest.mark.parametrize("suffix,body,method", REVISIONS)
@pytest.mark.parametrize(
    "extra", [{"expected_version": 0}, {"expected_version": True}, {"reason": " "}]
)
def test_revision_invalid_version_or_reason_never_reaches_service(
    signed_in, suffix, body, method, extra, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid revision reached service")

    monkeypatch.setattr(PostingService, method, forbidden)
    assert signed_in.post(LEDGER + suffix, json=body | extra).status_code == 422


def test_replacement_cannot_change_operation_identity(signed_in, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Replacement ID reached service")

    monkeypatch.setattr(PostingService, "correct_operation", forbidden)
    body = CORRECTION | {"replacement": CORRECTION["replacement"] | {"id": RECORD}}
    assert signed_in.post(LEDGER + REVISIONS[0][0], json=body).status_code == 422


@pytest.mark.parametrize("key", [None, "", "contains space", "a" * 129])
@pytest.mark.parametrize("suffix,body,method", WRITES + REVISIONS)
def test_missing_or_invalid_idempotency_key_never_posts(
    signed_in, monkeypatch, key, suffix, body, method
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid command reached service")

    monkeypatch.setattr(PostingService, method, forbidden)
    signed_in.headers.pop("idempotency-key")
    if key is not None:
        signed_in.headers["idempotency-key"] = key
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 422


@pytest.mark.parametrize(
    "extra",
    [{"amount": 10.0}, {"amount": "1e2"}, {"owner_id": str(OWNER)}, {"transaction_date": 0}],
)
def test_http_rejects_float_coercion_and_client_ownership(signed_in, monkeypatch, extra):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid command reached service")

    monkeypatch.setattr(PostingService, "post_opening", forbidden)
    assert signed_in.post(LEDGER + "/opening-balances", json=BASE | extra).status_code == 422


def test_financial_reads_forward_bounded_query_and_owner(signed_in, monkeypatch):
    calls = []

    def balances(self, owner_id, ledger_id, **kwargs):
        calls.append((owner_id, ledger_id, kwargs))
        return []

    monkeypatch.setattr(PostingService, "balances", balances)
    assert signed_in.get(LEDGER + f"/balances?account_id={RECORD}&limit=2&offset=3").json() == []
    assert calls == [(OWNER, UUID(RECORD), {"account_id": UUID(RECORD), "limit": 2, "offset": 3})]
    for suffix in ("/operations", "/balances", f"/operations/{RECORD}/history"):
        for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
            assert signed_in.get(LEDGER + suffix + "?" + query).status_code == 422
    assert len(calls) == 1


def test_current_state_and_history_queries_keep_filter_and_pagination(signed_in, monkeypatch):
    calls = []

    def records(self, owner_id, ledger_id, **kwargs):
        calls.append((owner_id, ledger_id, kwargs))
        return []

    def history(self, owner_id, ledger_id, operation_id, **kwargs):
        calls.append((owner_id, ledger_id, operation_id, kwargs))
        return []

    monkeypatch.setattr(PostingService, "list_operations", records)
    monkeypatch.setattr(PostingService, "history", history)
    assert signed_in.get(LEDGER + "/operations").json() == []
    assert calls[-1] == (
        OWNER,
        UUID(RECORD),
        {
            "limit": 100,
            "offset": 0,
            "status": "all",
            "order": "created_at",
            "from_date": None,
            "to_date": None,
        },
    )
    assert signed_in.get(LEDGER + "/operations?status=cancelled&limit=2&offset=1").json() == []
    assert calls[-1] == (
        OWNER,
        UUID(RECORD),
        {
            "limit": 2,
            "offset": 1,
            "status": "cancelled",
            "order": "created_at",
            "from_date": None,
            "to_date": None,
        },
    )
    assert signed_in.get(LEDGER + "/operations?status=unknown").status_code == 422
    assert signed_in.get(LEDGER + f"/operations/{RECORD}/history?limit=2&offset=1").json() == []
    assert calls[-1] == (OWNER, UUID(RECORD), UUID(RECORD), {"limit": 2, "offset": 1})


@pytest.mark.parametrize("order", ["created_at", "transaction_date"])
def test_current_state_query_forwards_date_order_bounds_and_existing_scope(
    signed_in, monkeypatch, order
):
    calls = []

    def records(self, owner_id, ledger_id, **kwargs):
        calls.append((owner_id, ledger_id, kwargs))
        return []

    monkeypatch.setattr(PostingService, "list_operations", records)
    response = signed_in.get(
        LEDGER + "/operations",
        params={
            "order": order,
            "from_date": "2026-01-02",
            "to_date": "2026-03-04",
            "status": "active",
            "limit": 2,
            "offset": 3,
        },
    )
    assert response.status_code == 200 and response.json() == []
    assert calls == [
        (
            OWNER,
            UUID(RECORD),
            {
                "limit": 2,
                "offset": 3,
                "status": "active",
                "order": order,
                "from_date": date(2026, 1, 2),
                "to_date": date(2026, 3, 4),
            },
        )
    ]


@pytest.mark.parametrize("parameter", ["from_date", "to_date"])
def test_current_state_query_allows_independent_date_bounds(signed_in, monkeypatch, parameter):
    calls = []

    def records(self, owner_id, ledger_id, **kwargs):
        calls.append((owner_id, ledger_id, kwargs))
        return []

    monkeypatch.setattr(PostingService, "list_operations", records)
    response = signed_in.get(LEDGER + "/operations", params={parameter: "2026-01-02"})
    assert response.status_code == 200 and response.json() == []
    owner, ledger, options = calls[0]
    assert (owner, ledger) == (OWNER, UUID(RECORD))
    assert options[parameter] == date(2026, 1, 2)
    other_bound = "to_date" if parameter == "from_date" else "from_date"
    assert options.get(other_bound) is None
    assert options["limit"] == 100 and options["offset"] == 0 and options["status"] == "all"


def test_current_state_query_accepts_a_single_day_closed_interval(signed_in, monkeypatch):
    calls = []

    def records(self, owner_id, ledger_id, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(PostingService, "list_operations", records)
    response = signed_in.get(
        LEDGER + "/operations", params={"from_date": "2026-01-02", "to_date": "2026-01-02"}
    )
    assert response.status_code == 200 and response.json() == []
    assert calls[0]["from_date"] == calls[0]["to_date"] == date(2026, 1, 2)


def test_current_state_query_rejects_reversed_date_interval(signed_in):
    response = signed_in.get(
        LEDGER + "/operations", params={"from_date": "2026-01-03", "to_date": "2026-01-02"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_date_range"


@pytest.mark.parametrize("order", ["unknown", "recognition_date", "CREATED_AT", "", " created_at"])
def test_current_state_query_rejects_unknown_order_before_service(signed_in, monkeypatch, order):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid order reached financial service")

    monkeypatch.setattr(PostingService, "list_operations", forbidden)
    response = signed_in.get(LEDGER + "/operations", params={"order": order})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "order"]


@pytest.mark.parametrize("parameter", ["from_date", "to_date"])
@pytest.mark.parametrize(
    "invalid_date",
    ["2026-02-30", "2026-13-01", "20260102", "2026-1-2", "2026-01-02T00:00:00Z", "0", ""],
)
def test_current_state_query_rejects_non_calendar_dates_before_service(
    signed_in, monkeypatch, parameter, invalid_date
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid calendar date reached financial service")

    monkeypatch.setattr(PostingService, "list_operations", forbidden)
    response = signed_in.get(LEDGER + "/operations", params={parameter: invalid_date})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", parameter]


@pytest.mark.parametrize("error_type,status", [(IntegrityError, 409), (OperationalError, 503)])
def test_database_errors_do_not_disclose_financial_values(
    signed_in, monkeypatch, caplog, error_type, status
):
    secret = "fictional-private-account-description"

    def unavailable(*args, **kwargs):
        raise error_type("private financial SQL", {"description": secret}, Exception(secret))

    monkeypatch.setattr(PostingService, "post_opening", unavailable)
    response = signed_in.post(LEDGER + "/opening-balances", json=BASE)
    assert response.status_code == status
    assert secret not in response.text + caplog.text
    assert "private financial SQL" not in response.text + caplog.text


def test_every_financial_write_is_guarded_and_documents_command_key(client):
    paths = client.get("/openapi.json").json()["paths"]
    writes = {
        path: specification
        for path, methods in paths.items()
        for method, specification in methods.items()
        if method in {"post", "patch", "put", "delete"}
        and "financial operations" in specification.get("tags", [])
    }
    assert set(writes) == {
        "/api/v1/ledgers/{ledger_id}" + suffix.replace(RECORD, "{operation_id}")
        for suffix, _, _ in WRITES + REVISIONS
    }
    for specification in writes.values():
        headers = {
            item["name"]: item for item in specification["parameters"] if item["in"] == "header"
        }
        assert headers["Idempotency-Key"]["required"] is True
        assert "X-CSRF-Token" in headers
