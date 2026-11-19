"""Whole-document writes share authenticated raw-intent, validation and private-error boundaries."""

import json
from uuid import UUID

import pytest
from coinpup_api.business.document_revision_receipt import DocumentRevisionResponse
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.business.document_revisions import BusinessDocumentRevisionService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_document_revision_receipt import sources_and_receipt
from tests.unit.business.test_settlement_router import HEADERS
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

PATHS = {"correct": "corrections", "cancel": "cancellations"}


def example(action):
    doc, _, proposed, saved = sources_and_receipt()
    saved.update(ledger_id=RECORD, action=action)
    values = dict(expected_version=doc.version, reason=saved["reason"])
    if action == "cancel":
        saved["replacement"] = None
        payload = ConfirmedDocumentCancellation(**values)
    else:
        payload = ConfirmedDocumentCorrection(**values, replacement=proposed)
    result = DocumentRevisionResponse.model_validate(saved)
    return f"{LEDGER}/business-documents/{doc.id}/{PATHS[action]}", payload, result


@pytest.mark.parametrize("action", PATHS)
def test_raw_json_reaches_only_the_authenticated_owner_and_scoped_document(
    signed_in, monkeypatch, action
):
    path, payload, result = example(action)
    calls = []

    def capture(self, owner, ledger, document, command, key, *, raw_body):
        calls.append((owner, ledger, document, command, key, raw_body))
        return result

    monkeypatch.setattr(BusinessDocumentRevisionService, action, capture)
    body = payload.model_dump(mode="json")
    for omitted in ("origin", "x-csrf-token"):
        denied = signed_in.post(
            path, json=body, headers={k: v for k, v in HEADERS.items() if k != omitted}
        )
        assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
    assert calls == []
    raw = json.dumps(body | {"reason": "  " + body["reason"] + "  "}, indent=2).encode()
    response = signed_in.post(path, content=raw, headers=HEADERS)
    assert response.status_code == 200 and response.json() == result.model_dump(mode="json")
    assert response.headers["cache-control"] == "no-store"
    assert calls == [
        (OWNER, UUID(RECORD), result.document_id, payload, HEADERS["Idempotency-Key"], raw)
    ]
    signed_in.cookies.clear()
    denied = signed_in.post(path, content=raw, headers=HEADERS)
    assert denied.status_code == 401 and len(calls) == 1


@pytest.mark.parametrize("action", PATHS)
def test_invalid_scopes_keys_and_commands_never_reach_the_service(signed_in, monkeypatch, action):
    path, payload, _ = example(action)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid document command reached the service")

    monkeypatch.setattr(BusinessDocumentRevisionService, action, forbidden)
    body = payload.model_dump(mode="json")
    for key in (None, "", "bad key", "x" * 129):
        headers = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
        if key is not None:
            headers["Idempotency-Key"] = key
        assert signed_in.post(path, json=body, headers=headers).status_code == 422
    invalid = [
        body | {"expected_version": True},
        body | {"owner_id": str(OWNER)},
        body | {"reason": ""},
    ]
    if action == "correct":
        invalid.extend(
            [
                body | {"replacement": body["replacement"] | {"lines": []}},
                body
                | {
                    "replacement": body["replacement"]
                    | {"lines": [body["replacement"]["lines"][0] | {"unit_price": 1}]}
                },
            ]
        )
    else:
        invalid.append(body | {"replacement": None})
    for value in invalid:
        response = signed_in.post(path, json=value, headers=HEADERS)
        assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
        assert all(set(v) == {"loc", "type", "msg"} for v in response.json()["detail"])
    assert signed_in.post(path, content="{", headers=HEADERS).status_code == 422
    invalid_path = path.replace(path.split("/")[-2], "invalid")
    assert signed_in.post(invalid_path, json=body, headers=HEADERS).status_code == 422


@pytest.mark.parametrize("action", PATHS)
@pytest.mark.parametrize(
    "status,code",
    [
        (404, "not_found"),
        (409, "document_revision_dependency"),
        (409, "period_closed"),
        (422, "amount_precision"),
        (503, "document_revision_integrity"),
    ],
)
def test_domain_errors_keep_stable_status_and_code(signed_in, monkeypatch, action, status, code):
    path, payload, _ = example(action)

    def fail(*args, **kwargs):
        raise LedgerError(code, status, "Fictional safe document error")

    monkeypatch.setattr(BusinessDocumentRevisionService, action, fail)
    response = signed_in.post(path, json=payload.model_dump(mode="json"), headers=HEADERS)
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("action", PATHS)
def test_storage_errors_never_expose_sql_or_connection_details(signed_in, monkeypatch, action):
    path, payload, _ = example(action)

    def fail(*args, **kwargs):
        raise SQLAlchemyError("Fictional private SQL and password")

    monkeypatch.setattr(BusinessDocumentRevisionService, action, fail)
    response = signed_in.post(path, json=payload.model_dump(mode="json"), headers=HEADERS)
    assert (
        response.status_code == 503 and response.json()["detail"]["code"] == "business_unavailable"
    )
    assert "SQL" not in response.text and "password" not in response.text
