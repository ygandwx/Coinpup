"""History GETs keep session scope, bounded pagination and the existing private error contract."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from coinpup_api.business.document_history import BusinessDocumentHistory, DocumentHistoryEntry
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_document_revision_router import example
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in


def request(method):
    _, _, fixed = example("correct")
    base = f"{LEDGER}/business-documents/{fixed.document_id}"
    if method == "get_revision":
        return (
            base + f"/revisions/{fixed.revision_id}",
            fixed,
            (OWNER, UUID(RECORD), fixed.document_id, fixed.revision_id),
            {},
        )
    item = DocumentHistoryEntry(
        version=fixed.previous_version,
        revision_id=None,
        action="confirm",
        reason=None,
        created_at=datetime.now(UTC),
        previous_line_count=None,
        replacement_line_count=None,
    )
    return (
        base + "/history?limit=1&offset=2",
        [item],
        (OWNER, UUID(RECORD), fixed.document_id),
        dict(limit=1, offset=2),
    )


@pytest.mark.parametrize("method", ["list_history", "get_revision"])
def test_authenticated_read_scope_and_pagination(signed_in, monkeypatch, method):
    path, result, args, kwargs = request(method)
    calls = []

    def capture(self, *actual, **options):
        calls.append((actual, options))
        return result

    monkeypatch.setattr(BusinessDocumentHistory, method, capture)
    response = signed_in.get(path)
    expected = (
        [r.model_dump(mode="json") for r in result]
        if isinstance(result, list)
        else result.model_dump(mode="json")
    )
    assert response.status_code == 200 and response.json() == expected
    assert response.headers["cache-control"] == "no-store" and calls == [(args, kwargs)]
    signed_in.cookies.clear()
    assert signed_in.get(path).status_code == 401 and len(calls) == 1


@pytest.mark.parametrize("method", ["list_history", "get_revision"])
@pytest.mark.parametrize(
    "error,status,code",
    [
        (LedgerError("not_found", 404, "Not found."), 404, "not_found"),
        (
            LedgerError("document_revision_integrity", 503, "Stored evidence is inconsistent."),
            503,
            "document_revision_integrity",
        ),
        (SQLAlchemyError("Fictional private SQL password"), 503, "business_unavailable"),
    ],
)
def test_history_errors_remain_safe(signed_in, monkeypatch, method, error, status, code):
    path, *_ = request(method)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(BusinessDocumentHistory, method, fail)
    response = signed_in.get(path)
    assert response.status_code == status and response.json()["detail"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text and "password" not in response.text


@pytest.mark.parametrize(
    "before,after,action",
    [
        (None, 2, "correct"),
        (0, 2, "correct"),
        (201, 2, "correct"),
        (2, 0, "correct"),
        (2, 2, "cancel"),
        (2, 1.0, "correct"),
    ],
)
def test_revision_directory_requires_both_exact_complete_counts(before, after, action):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DocumentHistoryEntry(
            version=3,
            revision_id=UUID(RECORD),
            action=action,
            reason="Fictional count",
            created_at=datetime.now(UTC),
            previous_line_count=before,
            replacement_line_count=after,
        )
