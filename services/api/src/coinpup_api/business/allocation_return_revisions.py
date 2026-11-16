"""Correct or cancel one complete allocation return under the serialized ledger transaction."""

from uuid import uuid4

from sqlalchemy import func, insert, select, update

from coinpup_api.business.allocation_return_revision_current import read_current_return
from coinpup_api.business.allocation_return_revision_hash import allocation_return_revision_hash_v2
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember,
)
from coinpup_api.business.allocation_return_revision_models import BusinessAllocationReturnRevision
from coinpup_api.business.allocation_return_revision_plan import plan_transition
from coinpup_api.business.allocation_return_revision_receipts import (
    replay_revision,
    response_from_rows,
)
from coinpup_api.business.allocation_return_sources import require_current
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.service import LedgerError, _version


class BusinessAllocationReturnRevisionService(BalanceQueries, PostingCore):
    def correct(self, owner_id, ledger_id, return_id, payload, key, *, raw_body=None):
        return self._revise(owner_id, ledger_id, return_id, "correct", payload, key, raw_body)

    def cancel(self, owner_id, ledger_id, return_id, payload, key, *, raw_body=None):
        return self._revise(owner_id, ledger_id, return_id, "cancel", payload, key, raw_body)

    def _revise(self, owner_id, ledger_id, return_id, action, payload, key, raw_body):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = allocation_return_revision_hash_v2(
                action, ledger_id, return_id, payload, raw_body=raw_body
            )
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_revision(
                    session, receipt, ledger_id, return_id, action, payload, digest
                )
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            current = read_current_return(session, ledger_id, return_id)
            _version(current, payload.expected_version)
            if current.cancelled:
                raise LedgerError(
                    "allocation_return_cancelled", 409, "A cancelled return cannot be changed."
                )
            require_current(current.payment_operation, 1, current.payment.journal_id, "payment")
            replacement = payload.replacement if action == "correct" else None
            try:
                catalog, sources, transitions = plan_transition(session, current, replacement)
                deltas = [
                    line
                    for member in transitions.values()
                    for group in (member.reversal, member.posting)
                    if group
                    for line in group.lines
                ]
                self._validate_balance_deltas(session, ledger_id, deltas, catalog)
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            identifier = uuid4()
            scope = dict(ledger_id=ledger_id, return_id=return_id, payment_id=current.payment.id)
            session.execute(
                insert(BusinessAllocationReturnRevision).values(
                    **scope,
                    id=identifier,
                    group_version=current.version + 1,
                    previous_revision_id=current.revision.id if current.revision else None,
                    action=action,
                    revision_reason=payload.reason,
                    return_date=replacement.return_date if replacement else None,
                    return_reason=replacement.reason if replacement else None,
                    created_by=owner_id,
                    idempotency_key=key,
                    created_at=now,
                )
            )
            for position, (source_id, member) in enumerate(transitions.items(), 1):
                _persist_member(
                    session,
                    scope,
                    identifier,
                    owner_id,
                    now,
                    payload.reason,
                    replacement,
                    position,
                    sources[source_id],
                    member,
                )
            result = response_from_rows(session, identifier)
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    hash_version=2,
                    request_hash=digest,
                    response_status=200,
                    response=result.model_dump(mode="json"),
                    operation_id=result.members[0].operation_id,
                    created_at=now,
                )
            )
            return result


def _persist_member(
    session, scope, revision_id, owner_id, now, reason, replacement, position, source, member
):
    ledger_id = scope["ledger_id"]
    old, reversal, posting, slot = member.old, member.reversal, member.posting, member.slot
    if old:
        operation_id = old.operation.id
        version = old.operation.version + 1
    else:
        operation_id = posting.operation_id
        version = 1
        session.execute(
            insert(FinancialOperation).values(
                id=operation_id,
                ledger_id=ledger_id,
                kind="unallocation",
                current_journal_id=posting.journal_id,
                created_by=owner_id,
                created_at=now,
                updated_at=now,
            )
        )
    sequence = slot.sequence
    session.execute(
        insert(BusinessReturnRevisionMember).values(
            **scope,
            id=uuid4(),
            revision_id=revision_id,
            member_no=position,
            action=("correct" if posting else "cancel") if old else "create",
            source_allocation_line_id=source.id,
            source_operation_id=source.operation_id,
            source_version=1,
            source_return_no=sequence.source_return_no,
            source_predecessor_operation_id=slot.predecessor,
            prefix_payment_amount=sequence.previous_payment.to_decimal(),
            prefix_document_amount=sequence.previous_document.to_decimal(),
            operation_id=operation_id,
            operation_version=version,
            previous_line_no=old.fact["line_no"] if old else None,
            previous_operation_version=old.operation.version if old else None,
            previous_journal_id=old.journal.id if old else None,
            reversal_journal_id=reversal.journal_id if reversal else None,
            line_no=position if posting else None,
            journal_id=posting.journal_id if posting else None,
            payment_amount=slot.replacement.payment.to_decimal() if posting else None,
            document_amount=slot.replacement.document.to_decimal() if posting else None,
            absorbs_remainder=slot.absorbs_remainder,
            created_at=now,
        )
    )
    for group in (reversal, posting):
        if group is None:
            continue
        reversing = group is reversal
        session.execute(
            insert(Journal).values(
                id=group.journal_id,
                ledger_id=ledger_id,
                operation_id=operation_id,
                operation_version=version,
                journal_kind="reversal" if reversing else "posting",
                reverses_journal_id=old.journal.id if reversing else None,
                description=group.description,
                revision_reason=reason if old else None,
                transaction_date=old.journal.transaction_date
                if reversing
                else replacement.return_date,
                recognition_date=old.journal.recognition_date
                if reversing
                else replacement.return_date,
                created_at=now,
            )
        )
        session.execute(insert(JournalLine), list(group.parameters))
    if old:
        session.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == operation_id, FinancialOperation.ledger_id == ledger_id)
            .values(
                version=version,
                status="active" if posting else "cancelled",
                current_journal_id=posting.journal_id if posting else old.journal.id,
                updated_at=now,
            )
        )
