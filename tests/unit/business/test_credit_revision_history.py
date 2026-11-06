"""Private history routes and actual-asset decoding keep immutable JSON precise."""

from datetime import UTC, datetime
from decimal import localcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.credit_revision_history_reads import BusinessCreditHistory, _receipt
from coinpup_api.business.credit_revision_history_schemas import CreditHistoryEntry
from coinpup_api.business.credit_revision_schemas import CreditRevisionResponse
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.business.test_credit_revision_contract import receipt
from tests.unit.ledger.test_ledger_api import LEDGER, OWNER, RECORD
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

CREDIT, REVISION = uuid4(), uuid4()
PATH = f"{LEDGER}/business-credits/{CREDIT}"


@pytest.mark.parametrize("detail", [False, True])
def test_history_reads_forward_only_session_scope_and_bounded_arguments(
    signed_in, monkeypatch, detail
):
    response = CreditRevisionResponse.model_validate(receipt())
    directory = CreditHistoryEntry(
        version=1,
        revision_id=None,
        action="create",
        reason="Fictional 中文",
        created_at=datetime.now(UTC),
    )
    result = response if detail else [directory]
    calls = []

    def capture(self, *args, **kwargs):
        calls.append((args, kwargs))
        return result

    method = "get_revision" if detail else "list_history"
    monkeypatch.setattr(BusinessCreditHistory, method, capture)
    suffix = f"/revisions/{REVISION}" if detail else "/history?limit=2&offset=3"
    actual = signed_in.get(PATH + suffix)
    assert actual.status_code == 200 and actual.headers["cache-control"] == "no-store"
    assert actual.json() == (
        response.model_dump(mode="json") if detail else [directory.model_dump(mode="json")]
    )
    assert (
        calls == [((OWNER, UUID(RECORD), CREDIT, REVISION), {})]
        if detail
        else calls == [((OWNER, UUID(RECORD), CREDIT), {"limit": 2, "offset": 3})]
    )
    signed_in.cookies.clear()
    denied = signed_in.get(PATH + suffix)
    assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
    assert len(calls) == 1


def test_history_invalid_parameters_do_not_reach_readers(signed_in, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid history parameters reached a reader")

    monkeypatch.setattr(BusinessCreditHistory, "list_history", forbidden)
    monkeypatch.setattr(BusinessCreditHistory, "get_revision", forbidden)
    for suffix in (
        "/history?limit=0",
        "/history?limit=201",
        "/history?offset=-1",
        "/history?offset=100001",
        "/revisions/not-a-uuid",
    ):
        response = signed_in.get(PATH + suffix)
        assert response.status_code == 422 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("failure", ["scope", "integrity", "storage"])
def test_history_errors_keep_the_existing_private_contract(signed_in, monkeypatch, detail, failure):
    def fail(*args, **kwargs):
        if failure == "storage":
            raise SQLAlchemyError("Fictional private connection details")
        raise LedgerError(
            "not_found" if failure == "scope" else "credit_revision_integrity",
            404 if failure == "scope" else 503,
            "Stored history is unavailable.",
        )

    monkeypatch.setattr(BusinessCreditHistory, "get_revision" if detail else "list_history", fail)
    response = signed_in.get(PATH + (f"/revisions/{REVISION}" if detail else "/history"))
    assert response.status_code == (404 if failure == "scope" else 503)
    assert (
        response.json()["detail"]["code"]
        == {
            "scope": "not_found",
            "integrity": "credit_revision_integrity",
            "storage": "business_unavailable",
        }[failure]
    )
    assert response.headers["cache-control"] == "no-store"
    assert "private connection" not in response.text


@pytest.mark.parametrize("overprecision", [False, True])
def test_fixed_detail_uses_actual_asset_without_decimal_context_or_string_rewriting(overprecision):
    value = receipt()
    if overprecision:
        value["members"][0].update(net_amount="17.231", tax_amount="2.769")
    parsed = CreditRevisionResponse.model_validate(value)
    saved = SimpleNamespace(
        id=parsed.revision_id,
        credit_id=parsed.credit_id,
        ledger_id=parsed.ledger_id,
        document_id=parsed.document_id,
    )
    original = SimpleNamespace(
        hash_version=2,
        response_status=200,
        operation_id=parsed.members[0].operation_id,
        response=value,
    )
    with localcontext() as context:
        context.prec = 1
        for signal in context.traps:
            context.traps[signal] = True
        if overprecision:
            with pytest.raises(LedgerError) as failure:
                _receipt(original, value, saved, ASSETS["USD"])
            assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)
        else:
            assert _receipt(original, value, saved, ASSETS["USD"]).model_dump(mode="json") == value
