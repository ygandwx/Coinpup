"""Issue an approved company Invoice or read its original state, within the owned ledger."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.invoice_issuance_reads import InvoiceIssuanceReads
from coinpup_api.business.invoice_issuance_results import (
    InvoiceIssuanceComplete,
    InvoiceIssuanceResult,
)
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_issuance_service import InvoiceIssuanceService
from coinpup_api.business.invoice_pdf import MAX_INVOICE_PDF_BYTES
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_invoice_issuance_router(settings, engine):
    router = APIRouter(
        prefix="/api/v1/ledgers/{ledger_id}/invoice-issuances", tags=["Invoice issuance"]
    )
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    original_body = Annotated[bytes, Depends(raw_command_body)]
    original_key = Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]{1,128}$"),
    ]
    reads = InvoiceIssuanceReads(engine)
    service = InvoiceIssuanceService(
        engine,
        store=FileStore(
            settings.files_directory, MAX_INVOICE_PDF_BYTES, settings.upload_timeout_seconds
        ),
        font_directory=settings.invoice_font_dir,
    )

    def failure(exc):
        if isinstance(exc, LedgerError):
            return HTTPException(exc.status, {"code": exc.code, "message": str(exc)})
        return HTTPException(
            503, {"code": "invoice_unavailable", "message": "Invoice storage is unavailable."}
        )

    @router.post("", status_code=201, response_model=InvoiceIssuanceComplete)
    async def issue(
        ledger_id: UUID,
        payload: InvoiceIssuanceCreate,
        identity: writer,
        idempotency_key: original_key,
        raw_body: original_body,
    ):
        """Finish the original reviewed intent. Only two sealed private originals yield success."""
        try:
            return await service.issue(
                identity.id, ledger_id, payload, idempotency_key, raw_body=raw_body
            )
        except (LedgerError, SQLAlchemyError) as error:
            raise failure(error) from None

    @router.get("/{issuance_id}", response_model=InvoiceIssuanceResult)
    def get(ledger_id: UUID, issuance_id: UUID, identity: reader):
        """Read the saved number and completion state without allocating or rendering."""
        try:
            return reads.get(identity.id, ledger_id, issuance_id)
        except (LedgerError, SQLAlchemyError) as error:
            raise failure(error) from None

    return router
