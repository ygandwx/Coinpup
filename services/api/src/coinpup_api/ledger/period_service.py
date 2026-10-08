"""Atomic optional period changes and permanent v2 original-intent replay."""

import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.ledger.common import PostingCore
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, _canonical_hash, _invalid_constant
from coinpup_api.ledger.period_models import LedgerPeriod, PeriodAudit, PeriodReceipt
from coinpup_api.ledger.period_schemas import PeriodChange, PeriodChangeResponse
from coinpup_api.ledger.service import LedgerError


def period_hash(ledger_id, payload, raw_body=None):
    try:
        provided = (
            payload.model_dump(mode="json", exclude_unset=True)
            if raw_body is None
            else json.loads(raw_body, parse_constant=_invalid_constant)
        )
        if PeriodChange.model_validate(provided) != payload:
            raise ValueError
        return _canonical_hash(
            {
                "action": "period-change",
                "route": "/api/v1/ledgers/{ledger_id}/period-changes",
                "ledger_id": str(ledger_id),
                "payload": provided,
            }
        )
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise LedgerError("invalid_request", 422, "The period command body is invalid.") from None


class PeriodService(PostingCore):
    def change(self, owner_id, ledger_id, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use a visible ASCII command key.")
        digest = period_hash(ledger_id, payload, raw_body)
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.scalar(
                select(PeriodReceipt).where(
                    PeriodReceipt.ledger_id == ledger_id, PeriodReceipt.idempotency_key == key
                )
            )
            if receipt is not None:
                if receipt.request_hash != digest:
                    raise LedgerError(
                        "idempotency_conflict",
                        409,
                        "This command key was used for a different request.",
                    )
                try:
                    response = PeriodChangeResponse.model_validate(receipt.response)
                    if (
                        receipt.hash_version != 2
                        or response.id != receipt.audit_id
                        or response.ledger_id != ledger_id
                    ):
                        raise ValueError
                    return response
                except (ValidationError, ValueError):
                    raise LedgerError(
                        "ledger_integrity", 503, "The stored period receipt is inconsistent."
                    ) from None
            state = session.scalar(select(LedgerPeriod).where(LedgerPeriod.ledger_id == ledger_id))
            version, old = (state.version, state.closed_through) if state else (1, None)
            if version != payload.expected_version:
                raise LedgerError(
                    "version_conflict", 409, "The period changed; reload before editing."
                )
            new = payload.closed_through
            valid = (
                (new is not None and (old is None or new >= old))
                if payload.action == "close"
                else (old is not None and (new is None or new < old))
            )
            if not valid:
                raise LedgerError(
                    "invalid_period_transition",
                    409,
                    "Closing must advance the cutoff; reopening must reduce it.",
                )
            if entity.archived:
                raise LedgerError(
                    "entity_archived", 409, "Restore the entity before changing its period."
                )
            if state is None:
                state = LedgerPeriod(
                    id=uuid4(), ledger_id=ledger_id, version=version + 1, closed_through=new
                )
                session.add(state)
            else:
                state.version += 1
                state.closed_through = new
            session.flush()
            audit = PeriodAudit(
                id=uuid4(),
                ledger_id=ledger_id,
                period_id=state.id,
                version=state.version,
                actor_id=owner_id,
                action=payload.action,
                reason=payload.reason,
                previous_closed_through=old,
                closed_through=new,
                created_at=session.scalar(select(func.clock_timestamp())),
            )
            session.add(audit)
            session.flush()
            response = PeriodChangeResponse.model_validate(audit)
            session.add(
                PeriodReceipt(
                    id=uuid4(),
                    ledger_id=ledger_id,
                    version=1,
                    audit_id=audit.id,
                    idempotency_key=key,
                    hash_version=2,
                    request_hash=digest,
                    response=response.model_dump(mode="json"),
                )
            )
            session.flush()
            return response
