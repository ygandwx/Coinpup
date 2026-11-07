"""Malformed loaded snapshots fail before a revision can assemble replacement rows."""

from contextlib import nullcontext
from copy import copy
from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest
from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_revision_current import read_current_credit
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.service import LedgerError


def loaded():
    ledger, credit_id, document_id, owner, operation_id, journal_id = [uuid4() for _ in range(6)]
    credit = BusinessCredit(
        id=credit_id,
        ledger_id=ledger,
        document_id=document_id,
        created_by=owner,
        credit_date=date(2026, 11, 6),
        reason="Fictional original 中文",
    )
    document = BusinessDocument(id=document_id, ledger_id=ledger, document_kind="invoice")
    operation = FinancialOperation(
        id=operation_id,
        ledger_id=ledger,
        kind="invoice_credit",
        created_by=owner,
        status="active",
        version=1,
        current_journal_id=journal_id,
    )
    journal = Journal(
        id=journal_id,
        ledger_id=ledger,
        operation_id=operation_id,
        operation_version=1,
        journal_kind="posting",
        reverses_journal_id=None,
        transaction_date=credit.credit_date,
        recognition_date=credit.credit_date,
        description=credit.reason,
        revision_reason=None,
    )
    fact = dict(
        ledger_id=ledger,
        credit_id=credit_id,
        document_id=document_id,
        revision_id=None,
        group_version=1,
        line_no=1,
        source_line_id=uuid4(),
        source_operation_id=uuid4(),
        operation_id=operation_id,
        operation_version=1,
        journal_id=journal_id,
        net_amount=Decimal("9"),
        tax_amount=Decimal("1"),
        total_amount=Decimal("10"),
        credit_date=credit.credit_date,
        credit_reason=credit.reason,
        recognition_date=credit.credit_date,
        posting_revision_reason=None,
    )
    lines = [
        JournalLine(journal_id=journal_id, ledger_id=ledger, line_no=position)
        for position in (1, 2)
    ]
    session = Mock()
    session.no_autoflush = nullcontext()
    session.scalar.return_value = None
    results = [Mock(), Mock(), Mock()]
    results[0].one_or_none.return_value = (credit, document)
    results[1].mappings.return_value = [fact]
    results[2].all.return_value = [(operation, journal)]
    session.execute.side_effect = results
    session.scalars.return_value.all.return_value = lines
    return session, results, credit, fact, operation, journal, lines


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "oversized",
        "position",
        "duplicate",
        "scope",
        "head",
        "version",
        "cancelled_operation",
        "author",
        "reason",
        "missing_line",
        "line_position",
    ],
)
def test_inconsistent_snapshot_is_a_specific_integrity_error_without_writes(fault):
    session, results, credit, fact, operation, journal, lines = loaded()
    if fault == "empty":
        results[1].mappings.return_value = []
    elif fault == "oversized":
        results[1].mappings.return_value = [fact] * 201
    elif fault == "position":
        fact["line_no"] = 2
    elif fault == "duplicate":
        duplicate = copy(fact)
        duplicate["line_no"] = 2
        results[1].mappings.return_value = [fact, duplicate]
    elif fault == "scope":
        fact["credit_id"] = uuid4()
    elif fault == "head":
        journal.id = uuid4()
    elif fault == "version":
        operation.version = 2
    elif fault == "cancelled_operation":
        operation.status = "cancelled"
    elif fault == "author":
        operation.created_by = uuid4()
    elif fault == "reason":
        journal.revision_reason = "Fictional unrelated revision"
    elif fault == "missing_line":
        lines.pop()
    else:
        lines[-1].line_no = 3
    with pytest.raises(LedgerError) as failure:
        read_current_credit(session, credit.ledger_id, credit.id)
    assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)
    for method in (session.flush, session.commit, session.add, session.delete):
        method.assert_not_called()


def test_terminal_header_cannot_hide_an_active_fact():
    session, _, credit, fact, *_ = loaded()
    revision = BusinessCreditRevision(
        id=uuid4(),
        credit_id=credit.id,
        ledger_id=credit.ledger_id,
        document_id=credit.document_id,
        group_version=2,
        action="cancel",
    )
    session.scalar.return_value = revision
    fact.update(group_version=2, revision_id=revision.id)
    with pytest.raises(LedgerError) as failure:
        read_current_credit(session, credit.ledger_id, credit.id)
    assert failure.value.code == "credit_revision_integrity"


def test_unknown_group_does_not_read_history():
    session, results, credit, *_ = loaded()
    results[0].one_or_none.return_value = None
    with pytest.raises(LedgerError) as failure:
        read_current_credit(session, credit.ledger_id, credit.id)
    assert (failure.value.code, failure.value.status) == ("not_found", 404)
    session.scalar.assert_not_called()
