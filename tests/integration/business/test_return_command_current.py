"""The authorized snapshot returns the exact sealed postings a later command must reverse."""

from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_current import read_current_return
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_fixed_replay import submit_fixture
from tests.integration.business.test_return_journal_transitions import group as group
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def read(s, identifier, expected_queries):
    with s["engine"].begin() as c, Session(bind=c) as session:
        c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        statements = []

        def count(conn, cursor, sql, parameters, context, many):
            statements.append(sql)

        event.listen(c, "before_cursor_execute", count)
        result = read_current_return(session, s["ledger"], identifier)
        assert len(statements) == expected_queries and all(
            q.startswith("SELECT") for q in statements
        )
        return result


@pytest.mark.parametrize("group", [("USD", "receipt"), ("EUR", "payout")], indirect=True)
def test_reads_each_current_version_without_intruding_on_past_receipts(group):
    setup, asset = group
    s, body, origin = setup
    v1 = read(s, origin.id, 4)
    assert v1.version == 1 and not v1.cancelled and v1.revision is None
    assert [m.journal.id for m in v1.members] == [p.journal_id for p in origin.postings]
    for m in v1.members:
        assert len(m.lines) == (2 if asset == "USD" else 4)
        assert all(line.journal_id == m.journal.id for line in m.lines)
    key, command, digest, expected = submit_fixture(setup, asset)
    v2 = read(s, origin.id, 4)
    assert v2.version == 2 and not v2.cancelled
    assert [str(m.journal.id) for m in v2.members] == [
        m["replacement"]["journal_id"] for m in expected["members"]
    ]
    assert [m.operation.id for m in v2.members] == list(
        reversed([m.operation.id for m in v1.members])
    )
    assert all(m.journal.sealed and m.journal.revision_reason == command.reason for m in v2.members)
    submit_fixture(setup, asset, prior=expected)
    s["structure"].update_asset(s["owner"], asset, AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    v3 = read(s, origin.id, 1)
    assert v3.version == 3 and v3.cancelled and v3.members == ()
    assert submit_return(s, body) == origin and snapshot(s) == before


def test_read_full_200_members_uses_the_same_four_selects(drafts):
    s = drafts
    _, _, _, body = setup_return(s, direction="receipt", asset="EUR", count=200)
    origin = submit_return(s, body)
    before = snapshot(s)
    state = read(s, origin.id, 4)
    assert [m.fact["source_allocation_line_id"] for m in state.members] == [
        p.source_allocation_line_id for p in origin.postings
    ]
    assert len(state.members) == 200 and sum(len(m.lines) for m in state.members) == 800
    assert {m.fact["payment_asset_id"] for m in state.members} == {"USD"}
    assert {m.fact["document_asset_id"] for m in state.members} == {"EUR"}
    assert snapshot(s) == before


def test_unknown_and_foreign_scopes_share_the_same_domain_error(group):
    (s, _, original), _ = group
    with Session(s["engine"]) as session:
        for ledger, target in ((uuid4(), original.id), (s["ledger"], uuid4())):
            with pytest.raises(LedgerError) as rejected:
                read_current_return(session, ledger, target)
            assert (rejected.value.status, rejected.value.code) == (404, "not_found")
