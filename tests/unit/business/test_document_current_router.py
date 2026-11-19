"""Current document GET uses authenticated scope and redacts storage failures."""

from uuid import UUID

import pytest
from coinpup_api.business.document_current import BusinessDocumentCurrentReads, DocumentCurrent
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

PATH = f"{LEDGER}/business-documents/{RECORD}/current"


def test_read_uses_session_scope_without_write_credentials(signed_in, monkeypatch):
    result = DocumentCurrent(
        ledger_id=RECORD,
        document_id=RECORD,
        confirmation_id=RECORD,
        document_kind="invoice",
        version=3,
        status="cancelled",
        revision_id=RECORD,
        document=None,
        postings=[],
    )
    calls = []

    def current(self, *args):
        calls.append(args)
        return result

    monkeypatch.setattr(BusinessDocumentCurrentReads, "current", current)
    response = signed_in.get(PATH)
    assert response.status_code == 200 and response.json() == result.model_dump(mode="json")
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, UUID(RECORD), UUID(RECORD))]
    signed_in.cookies.clear()
    assert signed_in.get(PATH).status_code == 401 and len(calls) == 1


@pytest.mark.parametrize(
    "error,status,code",
    [
        (LedgerError("not_found", 404, "Not found."), 404, "not_found"),
        (
            LedgerError(
                "document_revision_integrity", 503, "Stored document facts are inconsistent."
            ),
            503,
            "document_revision_integrity",
        ),
        (SQLAlchemyError("Fictional private SQL password"), 503, "business_unavailable"),
    ],
)
def test_read_preserves_safe_error_contract(signed_in, monkeypatch, error, status, code):
    def fail(*args):
        raise error

    monkeypatch.setattr(BusinessDocumentCurrentReads, "current", fail)
    response = signed_in.get(PATH)
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text and "password" not in response.text
