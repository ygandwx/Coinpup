"""Replay one device's original outcome or apply a witnessed, owner-scoped financial intent."""

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from coinpup_api.ledger.idempotency import _canonical_hash
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.sync.acknowledgements import DeviceAcknowledgements
from coinpup_api.sync.financial_commands import apply_financial_push
from coinpup_api.sync.financial_conflicts import competing_versions
from coinpup_api.sync.financial_push import decode_push_json, freeze_financial_push
from coinpup_api.sync.financial_sync_models import FinancialPushRecord as Record
from coinpup_api.sync.financial_sync_models import FinancialResolutionRecord as Resolution
from coinpup_api.sync.financial_sync_schemas import (
    FinancialConflictResolution,
    FinancialPushApplied,
    FinancialPushConflict,
    FinancialPushRequest,
    FinancialPushResponse,
)
from coinpup_api.sync.schemas import cursor_value

_RESPONSE = TypeAdapter(FinancialPushResponse)


def _outcome(row):
    try:
        reply = _RESPONSE.validate_python(row.response)
        if (
            reply.outcome != row.outcome
            or reply.device_id != row.device_id
            or reply.intent_id != row.intent_id
            or reply.ledger_id != row.ledger_id
            or reply.operation_id != row.operation_id
            or (
                isinstance(reply, FinancialPushApplied)
                and (reply.receipt.id, reply.receipt.ledger_id) != (row.operation_id, row.ledger_id)
            )
            or (
                isinstance(reply, FinancialPushConflict)
                and (reply.basis.version, reply.competing.version)
                != (row.basis_version, row.competing_version)
            )
        ):
            raise ValueError("Device outcome identity differs.")
    except ValueError:
        raise LedgerError(
            "ledger_integrity", 503, "The stored device outcome is inconsistent."
        ) from None
    return reply


class FinancialSyncService(DeviceAcknowledgements):
    def submit(self, owner, device, intent, raw_body):
        data = decode_push_json(raw_body)
        try:
            request = FinancialPushRequest.model_validate(data)
            digest = _canonical_hash(
                {
                    "protocol": "financial_push_v1",
                    "owner": str(owner),
                    "device": str(device),
                    "intent": str(intent),
                    "body": data,
                }
            )
        except (ValidationError, ValueError, RecursionError):
            raise LedgerError(
                "invalid_financial_push", 422, "Expected a bounded financial push command."
            ) from None

        push = freeze_financial_push(request.ledger_id, data["command"])
        with self._transaction(owner, write=True, read_committed=True) as session:
            self._ledger(session, owner, request.ledger_id)
            existing = session.get(Record, (owner, device, intent))
            if existing is not None:
                if existing.ledger_id != request.ledger_id:
                    raise _not_found()
                if existing.request_hash != digest:
                    raise LedgerError(
                        "idempotency_conflict", 409, "The original device intent differs."
                    )
                return _outcome(existing)

            current = self.checkpoint_for(session, owner, device, require_active=True)
            if request.checkpoint.cursor != current.cursor:
                raise LedgerError(
                    "sync_checkpoint_conflict", 409, "The device checkpoint has changed."
                )
            self._require_anchor(
                session, owner, cursor_value(current.cursor), request.checkpoint.witness
            )
            identity = {
                "intent_id": intent,
                "device_id": device,
                "ledger_id": request.ledger_id,
                "operation_id": push.operation_id,
            }
            receipt_key, basis, competing = None, None, None
            try:
                receipt = apply_financial_push(session, owner, push, request.idempotency_key)
            except LedgerError as error:
                if error.code != "version_conflict":
                    raise
                evidence = competing_versions(session, push)
                if evidence is None:
                    raise
                basis, competing = evidence.basis.version, evidence.competing.version
                response = FinancialPushConflict(
                    **identity,
                    outcome="conflict",
                    command_kind=push.command.kind,
                    original_payload=push.original_payload.decode("utf-8"),
                    basis=evidence.basis,
                    competing=evidence.competing,
                )
            else:
                receipt_key = request.idempotency_key
                response = FinancialPushApplied(**identity, outcome="applied", receipt=receipt)

            row = Record(
                owner_id=owner,
                **identity,
                command_key=request.idempotency_key,
                request_hash=digest,
                ledger_digest=push.ledger_digest,
                request_body=raw_body.decode("utf-8"),
                receipt_key=receipt_key,
                basis_version=basis,
                competing_version=competing,
                outcome=response.outcome,
                response=response.model_dump(mode="json"),
            )
            session.add(row)
            session.flush()
            return response

    def _owned(self, session, owner, device, intent):
        row = session.get(Record, (owner, device, intent))
        if row is None:
            raise _not_found()
        self._ledger(session, owner, row.ledger_id)
        return row

    def get(self, owner, device, intent):
        with self._transaction(owner, read_only=True) as session:
            return _outcome(self._owned(session, owner, device, intent))

    def get_resolution(self, owner, device, intent):
        with self._transaction(owner, read_only=True) as session:
            self._owned(session, owner, device, intent)
            row = session.get(Resolution, (owner, device, intent))
            return FinancialConflictResolution.model_validate(row) if row is not None else None

    def keep_current(self, owner, device, intent, expected):
        with self._transaction(owner, write=True, read_committed=True) as session:
            result = self._owned(session, owner, device, intent)
            if result.outcome != "conflict":
                raise LedgerError("sync_push_not_conflict", 409, "The push has no conflict.")
            existing = session.get(Resolution, (owner, device, intent))
            if existing is not None:
                if existing.accepted_version != expected:
                    raise LedgerError("idempotency_conflict", 409, "The original choice differs.")
                return FinancialConflictResolution.model_validate(existing)
            self.checkpoint_for(session, owner, device, require_active=True)
            current = session.scalar(
                select(FinancialOperation)
                .where(
                    FinancialOperation.id == result.operation_id,
                    FinancialOperation.ledger_id == result.ledger_id,
                )
                .with_for_update()
            )
            if current is None or current.version != expected:
                raise LedgerError("version_conflict", 409, "The current financial version differs.")
            row = Resolution(
                owner_id=owner, device_id=device, intent_id=intent, accepted_version=expected
            )
            session.add(row)
            session.flush()
            return FinancialConflictResolution.model_validate(row)
