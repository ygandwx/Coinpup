"""A single ledger snapshot shares current return facts across capacity readers and the writer."""

from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_projections import return_source_state
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.settlement_totals import applied_totals, document_totals, payment_totals
from coinpup_api.ledger.models import FinancialOperation
from sqlalchemy import event, insert

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("action,expected", [("correct", "87.66"), ("cancel", "100")])
def test_every_scoped_capacity_uses_the_same_revision_while_creation_is_immutable(
    revision, action, expected
):
    s, header, _, original = revision
    line = original.postings[0]
    row = member(header, original)
    if action == "cancel":
        header.update(action="cancel", return_date=None, return_reason=None)
        row.update(
            action="cancel",
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member).values(**row))
        assert document_totals(c, s["ledger"], [line.document_line_id]) == {
            line.document_line_id: (Decimal(expected), 0, 0)
        }
        assert payment_totals(c, s["ledger"], [original.payment_id]) == {
            original.payment_id: (Decimal(expected), 0)
        }
        assert applied_totals(c, s["ledger"], [original.payment_id]) == {
            original.payment_id: Decimal(expected)
        }
        assert applied_totals(c, s["ledger"], [line.document_line_id], by_document_line=True) == {
            line.document_line_id: Decimal(expected)
        }
        remaining = Decimal("100") - Decimal(expected)
        assert return_source_state(c, s["ledger"], [line.source_allocation_line_id]) == {
            line.source_allocation_line_id: (remaining, remaining, 1)
        }
        assert document_totals(c, uuid4(), [line.document_line_id]) == {}
        assert payment_totals(c, uuid4(), [original.payment_id]) == {}
        assert return_source_state(c, uuid4(), [line.source_allocation_line_id]) == {}
        c.rollback()
    assert snapshot(s) == before


def test_later_creation_slot_remains_reserved_after_its_removal_and_never_adds_live_quantity(
    revision,
):
    s, header, _, original = revision
    line = original.postings[0]
    before = snapshot(s)
    with s["engine"].connect() as c:
        # Stage a second lifecycle for this source without committing any new financial operation.
        # A historical created slot must still count after its group is cancelled.
        r = member(header, original) | dict(
            action="create",
            source_return_no=2,
            source_predecessor_operation_id=line.operation_id,
            previous_line_no=None,
            previous_operation_version=None,
            previous_journal_id=None,
            reversal_journal_id=None,
            operation_version=1,
            prefix_payment_amount=20,
            prefix_document_amount=20,
        )
        new_op = uuid4()
        r["operation_id"] = new_op
        c.execute(
            insert(FinancialOperation).values(
                id=new_op,
                ledger_id=s["ledger"],
                kind="unallocation",
                created_by=s["owner"],
                current_journal_id=r["journal_id"],
            )
        )
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member).values(**r))
        last = header | dict(
            id=uuid4(),
            previous_revision_id=header["id"],
            group_version=3,
            action="cancel",
            return_date=None,
            return_reason=None,
            idempotency_key="fictional-terminal",
        )
        c.execute(insert(Revision).values(**last))
        c.execute(
            insert(Member).values(
                **(
                    r
                    | dict(
                        id=uuid4(),
                        revision_id=last["id"],
                        action="cancel",
                        previous_line_no=1,
                        previous_operation_version=1,
                        operation_version=2,
                        previous_journal_id=r["journal_id"],
                        reversal_journal_id=uuid4(),
                        line_no=None,
                        journal_id=None,
                        payment_amount=None,
                        document_amount=None,
                        absorbs_remainder=None,
                    )
                )
            )
        )
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(c, "before_cursor_execute", record)
        assert return_source_state(c, s["ledger"], iter([])) == {}
        assert return_source_state(c, s["ledger"], [line.source_allocation_line_id]) == {
            line.source_allocation_line_id: (0, 0, 2)
        }
        assert len(statements) == 1
        c.rollback()
    assert snapshot(s) == before
