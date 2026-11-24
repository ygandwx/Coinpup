"""Seal both owned file rows and their fixed receipt in one short transaction."""

from uuid import uuid4

from sqlalchemy import func

from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_file_choices import InvoiceFileChoices, _select_choices
from coinpup_api.business.invoice_issuance_reads import _read_result
from coinpup_api.business.invoice_issuance_results import (
    InvoiceIssuanceComplete,
    InvoicePdfArtifact,
)
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.common import PostingCore
from coinpup_api.ledger.service import LedgerError


def _seal(session, *, owner, ledger, entity, identifier, prepared, verified):
    """Caller owns the write lock and ledger/entity locks; actual bytes were checked outside."""
    if entity.kind != "company":
        raise LedgerError("invoice_requires_company", 422, "Invoice issuance requires a company.")
    current = _read_result(session, owner, ledger, identifier)
    if current.state == "complete":
        # Another finalizer already committed. Any extra published blobs remain private orphans.
        return current
    # A previously authorized reservation stays bound to its original input after archival.
    actual = _select_choices(session, owner, ledger, identifier, prepared)
    if type(verified) is not InvoiceFileChoices or verified != actual:
        raise LedgerError(
            "invoice_file_choice_changed",
            409,
            "Verify the current canonical Invoice originals again.",
        )
    artifacts = []
    for choice in actual.artifacts:
        file_id = choice.existing_file_id
        if file_id is None:
            file_id = uuid4()
            name = f"invoice-{identifier}-{choice.language}.pdf"
            session.add(
                StoredFile(
                    id=file_id,
                    ledger_id=ledger,
                    created_by=owner,
                    blob_key=choice.blob_key,
                    sha256=choice.sha256,
                    byte_size=choice.byte_size,
                    detected_media_type="application/pdf",
                    original_filename=name,
                    title=name,
                )
            )
        artifacts.append(
            InvoicePdfArtifact(
                language=choice.language,
                file_id=file_id,
                sha256=choice.sha256,
                byte_size=choice.byte_size,
                media_type="application/pdf",
            )
        )
    session.flush()
    expected = InvoiceIssuanceComplete(
        **current.model_dump(exclude={"state", "artifacts"}),
        state="complete",
        artifacts=tuple(artifacts),
    )
    session.add(
        InvoiceCompletion(
            id=identifier,
            ledger_id=ledger,
            created_by=owner,
            created_at=func.clock_timestamp(),
            zh_file_id=artifacts[0].file_id,
            en_file_id=artifacts[1].file_id,
            response_status=201,
            response=expected.model_dump(mode="json"),
        )
    )
    session.flush()
    # Read independently from stored rows; don't return the just-constructed response.
    return _read_result(session, owner, ledger, identifier)


class InvoiceFinalization(PostingCore):
    def seal(self, owner, ledger, identifier, *, prepared, verified):
        """Internal handoff from the trusted renderer/byte verifier; no client-supplied choices."""
        with self._transaction(owner, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner, ledger)
            return _seal(
                session,
                owner=owner,
                ledger=ledger,
                entity=entity,
                identifier=identifier,
                prepared=prepared,
                verified=verified,
            )
