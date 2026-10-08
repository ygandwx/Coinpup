"""Read-only evidence API keeps cookie, bounds and redacted-error guarantees."""

import pytest
from coinpup_api.ocr.draft_reads import DraftReadService, DraftSummary
from sqlalchemy.exc import OperationalError

from tests.unit.ocr.test_ocr_router import LEDGER, OTHER, OWNER, SQL, TOKEN, job_view
from tests.unit.ocr.test_ocr_router import client as client

PREFIX = f"/api/v1/ledgers/{LEDGER}/ocr-drafts"


def summary():
    job = job_view()
    return DraftSummary(
        id=OTHER,
        ledger_id=LEDGER,
        job_id=job.id,
        file_id=job.file_id,
        source_key="document:0",
        status="draft",
        version=1,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@pytest.mark.parametrize("suffix,method", [("", "list_drafts"), (f"/{OTHER}", "get_draft")])
def test_evidence_authentication_precedes_service(client, monkeypatch, suffix, method):
    monkeypatch.setattr(
        DraftReadService, method, lambda *_args, **_kwargs: pytest.fail("Unauthenticated read")
    )
    response = client.get(PREFIX + suffix)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "offset=-1", "offset=100001", "status=confirmed", "job_id=not-a-uuid"],
)
def test_pagination_and_filter_validation_precedes_storage(client, monkeypatch, query):
    client.cookies.set("coinpup_session", TOKEN)
    monkeypatch.setattr(
        DraftReadService, "list_drafts", lambda *_args, **_kwargs: pytest.fail("Invalid filter")
    )
    assert client.get(PREFIX + "?" + query).status_code == 422


def test_owner_from_session_and_explicit_paging_are_forwarded(client, monkeypatch):
    client.cookies.set("coinpup_session", TOKEN)
    received = []

    def listing(self, owner, ledger, **kwargs):
        received.append((owner, ledger, kwargs))
        return [summary()]

    monkeypatch.setattr(DraftReadService, "list_drafts", listing)
    response = client.get(
        PREFIX, params={"job_id": str(OTHER), "status": "ignored", "limit": 1, "offset": 2}
    )
    assert response.status_code == 200
    assert received == [
        (OWNER, LEDGER, {"job_id": OTHER, "status": "ignored", "limit": 1, "offset": 2})
    ]
    assert response.json() == [summary().model_dump(mode="json")]
    assert not {"fields", "recognition", "lease_token", "configuration"} & response.json()[0].keys()


@pytest.mark.parametrize("suffix,method", [("", "list_drafts"), (f"/{OTHER}", "get_draft")])
def test_database_errors_never_return_originals_or_sql(client, monkeypatch, suffix, method):
    client.cookies.set("coinpup_session", TOKEN)

    def fail(*_args, **_kwargs):
        raise OperationalError(SQL, {}, RuntimeError("Fictional private invoice"))

    monkeypatch.setattr(DraftReadService, method, fail)
    response = client.get(PREFIX + suffix)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "ocr_unavailable"
    assert "Fictional" not in response.text and SQL not in response.text


def test_reads_do_not_offer_mutating_methods(client):
    client.cookies.set("coinpup_session", TOKEN)
    assert client.post(PREFIX, json={}).status_code == 405
    assert client.patch(f"{PREFIX}/{OTHER}", json={}).status_code == 405
