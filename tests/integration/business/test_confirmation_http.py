"""Fictional confirmation API lifecycle and bounded sealed reads on PostgreSQL."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from sqlalchemy import event

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_http_confirmation_sealed_reads_and_unknown_submission_replay(
    drafts, authenticated_client, kind
):
    s = drafts
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    saved, payload = prepare(s, kind=kind)
    base = f"/api/v1/ledgers/{s['ledger']}"
    path = base + f"/business-documents/{saved.id}"
    assert client.get(path).json() == saved.model_dump(mode="json")
    raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
    headers = {"Idempotency-Key": "fictional-http-confirm", "Content-Type": "application/json"}
    response = client.post(path + "/confirm", content=raw, headers=headers)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    receipt = response.json()
    before = snapshot(s)
    replay = client.post(path + "/confirm", content=raw, headers=headers)
    assert replay.status_code == 201 and replay.json() == receipt
    detail = client.get(path + "/confirmation").json()
    assert detail["confirmation"] == receipt
    document = detail["document"]
    assert document["state"] == "confirmed" and document["version"] == saved.version + 1
    assert document["lines"] == [line.model_dump(mode="json") for line in saved.lines]
    assert (document["net_amount"], document["tax_amount"], document["total_amount"]) == (
        "100.00",
        "10.00",
        "110.00",
    )
    assert client.get(base + "/business-confirmations").json() == [
        {key: value for key, value in document.items() if key != "lines"}
    ]
    assert client.get(path).status_code == 404
    assert client.get(base + "/business-documents").json() == []
    assert client.get(base + "/operations").json() == []
    assert (
        client.get(base + "/operations/" + receipt["postings"][0]["operation_id"]).status_code
        == 404
    )
    assert snapshot(s) == before
    changed = client.post(
        path + "/confirm",
        json=payload.model_dump(mode="json") | {"expected_version": 2},
        headers=headers,
    )
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_conflict"
    repeated = client.post(
        path + "/confirm", content=raw, headers=headers | {"Idempotency-Key": "fictional-new-key"}
    )
    assert repeated.status_code == 409 and repeated.json()["detail"]["code"] == "document_confirmed"
    assert snapshot(s) == before
    change_period(s, cutoff="2026-10-31")
    s["master"].update_party(
        owner,
        s["ledger"],
        s["party"].id,
        PartyUpdate(expected_version=1, archived=True, name="Fictional renamed"),
    )
    s["master"].update_project(
        owner, s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(owner, "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        owner, s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert client.get(path + "/confirmation").json() == detail
    assert client.post(path + "/confirm", content=raw, headers=headers).json() == receipt
    assert snapshot(s) == before


def test_confirmation_reads_are_scoped_and_list_queries_are_bounded(drafts):
    s = drafts
    first, first_payload = prepare(s)
    second, second_payload = prepare(s)
    submit(s, first, first_payload, "fictional-first")
    submit(s, second, second_payload, "fictional-second")
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    reads = BusinessConfirmationReads(s["engine"])
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", record)
    try:
        rows = reads.list_confirmations(s["owner"], s["ledger"])
    finally:
        event.remove(s["engine"], "before_cursor_execute", record)
    assert [row.id for row in rows] == [first.id, second.id]
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 6
    assert not any("business_confirmations.snapshot" in sql for sql in statements)
    assert [r.id for r in reads.list_confirmations(s["owner"], s["ledger"], limit=1, offset=1)] == [
        second.id
    ]
    assert reads.list_confirmations(s["owner"], other.ledger.id) == []
    from coinpup_api.ledger.service import LedgerError

    before = snapshot(s)
    for owner, ledger, identifier in [
        (s["owner"], other.ledger.id, first.id),
        (uuid4(), s["ledger"], first.id),
        (s["owner"], s["ledger"], uuid4()),
    ]:
        with pytest.raises(LedgerError) as error:
            reads.get_confirmation(owner, ledger, identifier)
        assert error.value.status == 404
    assert snapshot(s) == before
