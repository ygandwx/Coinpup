"""Replace all 200 returned allocations by 200 other sources of the same EUR documents."""

from uuid import UUID, uuid4

import pytest
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import EntityUpdate
from sqlalchemy import func, select

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_http import send
from tests.integration.business.test_return_revision_service import cancel, command
from tests.integration.business.test_settlement_service import allocate, pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def two_disjoint_allocations(s):
    payment, template = prepared(s, direction="receipt", asset="EUR", initial=False, count=200)
    pay(s, payment)
    source_groups = []
    for number in (1, 2):
        group = BusinessAllocationCreate.model_validate(
            template.model_dump(mode="json")
            | {
                "id": uuid4(),
                "lines": [
                    line.model_dump(mode="json") | {"payment_amount": "55", "document_amount": "50"}
                    for line in template.lines
                ],
            }
        )
        allocate(s, group, f"fictional-four-hundred-group-{number}")
        with s["engine"].connect() as db:
            source_groups.append(
                list(
                    db.scalars(
                        select(BusinessAllocationLine.id)
                        .where(BusinessAllocationLine.allocation_id == group.id)
                        .order_by(BusinessAllocationLine.line_no)
                    )
                )
            )
    assert len(source_groups[0]) == len(source_groups[1]) == 200
    assert len(set(source_groups[0] + source_groups[1])) == 400
    return payment.id, source_groups


def test_full_disjoint_return_records_and_replays_four_hundred_members(
    drafts, authenticated_client
):
    s = drafts
    client, _, _ = authenticated_client
    payment, (old, new) = two_disjoint_allocations(s)

    def line(source, p, d):
        return dict(
            source_allocation_line_id=source,
            expected_version=1,
            payment_amount=p,
            document_amount=d,
        )

    body = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=payment,
        return_date="2026-10-01",
        reason="Fictional original 200 allocations",
        lines=[line(source, "22", "20") for source in old],
    )
    change = command(
        body,
        lines=[
            body.lines[0].model_copy(update=line(source, "11", "10")) for source in reversed(new)
        ],
    )
    scope = f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns"
    target = f"{scope}/{body.id}"

    def post(path, request, key, expected):
        reply = send(client, path, request.model_dump(mode="json"), key)
        assert reply.status_code == expected, reply.text[:600]
        return reply.json()

    created = post(scope, body, "fictional-400-original", 201)
    corrected = post(target + "/corrections", change, "fictional-400-correct", 200)
    members = corrected["members"]
    assert len(members) == 400
    added, retired = members[:200], members[200:]
    assert [m["source_allocation_line_id"] for m in added] == list(map(str, reversed(new)))
    assert [m["source_allocation_line_id"] for m in retired] == list(map(str, old))
    assert [m["operation_id"] for m in retired] == [m["operation_id"] for m in created["postings"]]
    assert [m["previous"]["journal_id"] for m in retired] == [
        m["journal_id"] for m in created["postings"]
    ]
    assert [m["previous"]["line_no"] for m in retired] == list(range(1, 201))
    assert [m["replacement"]["line_no"] for m in added] == list(range(1, 201))
    assert len({m["operation_id"] for m in members}) == 400
    assert all(m["document_asset_id"] == "EUR" and m["source_version"] == 1 for m in members)
    assert all(
        m["action"] == "create"
        and m["operation_version"] == 1
        and m["previous"] is None
        and m["reversal_journal_id"] is None
        and m["replacement"]["payment_amount"] == "11.000000000000000000"
        and m["replacement"]["document_amount"] == "10.000000000000000000"
        for m in added
    )
    assert all(
        m["action"] == "cancel"
        and m["operation_version"] == 2
        and m["replacement"] is None
        and m["previous"]["payment_amount"] == "22.000000000000000000"
        and m["previous"]["document_amount"] == "20.000000000000000000"
        for m in retired
    )
    journals = [UUID(m["replacement"]["journal_id"]) for m in added] + [
        UUID(m["reversal_journal_id"]) for m in retired
    ]
    assert len(set(journals)) == 400
    with s["engine"].connect() as db:
        balances = db.execute(
            select(JournalLine.journal_id, JournalLine.asset_id, func.sum(JournalLine.amount))
            .where(JournalLine.ledger_id == s["ledger"], JournalLine.journal_id.in_(journals))
            .group_by(JournalLine.journal_id, JournalLine.asset_id)
        ).all()
    assert {(journal, asset) for journal, asset, _ in balances} == {
        (journal, asset) for journal in journals for asset in ("USD", "EUR")
    }
    assert all(total == 0 for _, _, total in balances)
    current = client.get(target + "/current").json()
    assert [m["operation_id"] for m in current["members"]] == [m["operation_id"] for m in added]
    end = cancel(2)
    ended = post(target + "/cancellations", end, "fictional-400-cancel", 200)
    assert [m["previous"] for m in ended["members"]] == [m["replacement"] for m in added]
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert post(scope, body, "fictional-400-original", 201) == created
    assert post(target + "/corrections", change, "fictional-400-correct", 200) == corrected
    assert post(target + "/cancellations", end, "fictional-400-cancel", 200) == ended
    assert client.get(target + "/revisions/" + corrected["revision_id"]).json() == corrected
    assert client.get(target + "/revisions/" + ended["revision_id"]).json() == ended
    assert [row["version"] for row in client.get(target + "/history").json()] == [1, 2, 3]
    assert snapshot(s) == before
