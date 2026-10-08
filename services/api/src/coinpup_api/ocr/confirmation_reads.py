"""Owner-scoped historical receipts and advisory exact-source duplicate hints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select

from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.service import LedgerService, _not_found

from .confirmation import duplicate_confirmations, replay
from .models import OcrConfirmation
from .review import scoped_draft


class DuplicateConfirmation(BaseModel):
    draft_id: UUID
    ledger_id: UUID
    target_ledger_id: UUID
    operation_id: UUID
    created_at: datetime


class ConfirmationReadService(LedgerService):
    def get_confirmation(self, owner_id, ledger_id, draft_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            receipt = session.scalar(
                select(OcrConfirmation).where(
                    OcrConfirmation.created_by == owner_id,
                    OcrConfirmation.ledger_id == ledger_id,
                    OcrConfirmation.draft_id == draft_id,
                )
            )
            if receipt is None:
                raise _not_found()
            return replay(receipt, receipt.request_hash)

    def list_duplicates(self, owner_id, ledger_id, draft_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            draft, job = scoped_draft(session, owner_id, ledger_id, draft_id)
            source = session.scalar(
                select(StoredFile).where(
                    StoredFile.id == job.file_id,
                    StoredFile.ledger_id == ledger_id,
                    StoredFile.created_by == owner_id,
                )
            )
            if source is None:
                raise _not_found()
            return [
                DuplicateConfirmation(
                    draft_id=row.draft_id,
                    ledger_id=row.ledger_id,
                    target_ledger_id=row.target_ledger_id,
                    operation_id=row.operation_id,
                    created_at=row.created_at,
                )
                for row in duplicate_confirmations(session, owner_id, draft, source)
            ]
