"""History is a bounded metadata directory; each detail is always its original fixed receipt."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate, EntityUpdate

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_revision_service import cancel, command, run
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction,asset,count", [("payout", "USD", 2), ("receipt", "EUR", 200)])
def test_closed_group_details_match_each_original_receipt(
    drafts, authenticated_client, direction, asset, count
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, _, body = setup_return(s, direction=direction, asset=asset, count=count)
    original = submit_return(s, body)
    base = f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns/{original.id}"
    edited = run(s, original.id, command(body))
    ended = run(s, original.id, cancel(2), "fictional-history-end")
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    response = client.get(base + "/history")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    directory = response.json()
    assert [r["version"] for r in directory] == [1, 2, 3]
    assert [r["action"] for r in directory] == ["create", "correct", "cancel"]
    assert [r["reason"] for r in directory] == [body.reason, edited.reason, ended.reason]
    assert [r["revision_id"] for r in directory] == [
        None,
        str(edited.revision_id),
        str(ended.revision_id),
    ]
    assert all(
        set(r) == {"version", "action", "reason", "created_at", "revision_id"} for r in directory
    )
    for offset, limit in ((0, 1), (1, 1), (1, 2), (2, 200), (100000, 200)):
        assert (
            client.get(base + f"/history?limit={limit}&offset={offset}").json()
            == directory[offset : offset + limit]
        )
    for intent in [edited, ended]:
        receipt = client.get(base + f"/revisions/{intent.revision_id}")
        assert receipt.status_code == 200 and receipt.headers["cache-control"] == "no-store"
        assert (
            receipt.json() == intent.model_dump(mode="json")
            and len(receipt.json()["members"]) == count
        )
    assert (
        client.get(base).json()["receipt"] == original.model_dump(mode="json")
        and snapshot(s) == before
    )


def test_history_pagination_and_foreign_revision_lookup_leave_no_traces(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, _, body = setup_return(s)
    original = submit_return(s, body)
    other_group = submit_return(
        s, body.model_copy(update={"id": uuid4()}), "fictional-second-return"
    )
    other_ledger = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other scope",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    edited = run(s, original.id, command(body))
    base = f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns/{original.id}"
    path = f"{base}/revisions/{edited.revision_id}"
    before = snapshot(s)
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert client.get(base + "/history?" + query).status_code == 422
    for route in (
        path.replace(str(original.id), str(other_group.id)),
        path.replace(str(s["ledger"]), str(other_ledger.ledger.id)),
        path.replace(str(edited.revision_id), str(uuid4())),
        base.replace(str(original.id), str(uuid4())) + "/history",
    ):
        rejected = client.get(route)
        assert rejected.status_code == 404 and rejected.json()["detail"]["code"] == "not_found"
        assert rejected.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert client.get(path).status_code == client.get(base + "/history").status_code == 401
    assert snapshot(s) == before
