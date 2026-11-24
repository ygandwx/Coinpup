"""Run one reviewed issuance intent without holding a database transaction across rendering."""

from functools import partial

import anyio

from coinpup_api.business.invoice_artifacts import prepare_invoice_blobs
from coinpup_api.business.invoice_file_choices import InvoiceOriginalFiles
from coinpup_api.business.invoice_issuance_results import (
    InvoiceIssuanceComplete,
    InvoiceIssuanceIdentity,
    InvoiceIssuancePending,
    verify_issuance_result_identity,
)
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_metadata_finalize import InvoiceFinalization
from coinpup_api.business.invoice_render_process import check_invoice_logo
from coinpup_api.business.invoice_render_sources import InvoiceRenderSources, _read_original
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.business.invoice_reservation_inputs import FreshInvoiceLogo, InvoiceLogoCheck
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError


def _integrity():
    return LedgerError(
        "invoice_issuance_integrity", 503, "The original Invoice result could not be verified."
    )


def _receipt(receipt, identity):
    try:
        return verify_issuance_result_identity(receipt, identity)
    except (TypeError, ValueError, AttributeError):
        raise _integrity() from None


def _check_logo(store, source):
    try:
        return InvoiceLogoCheck(
            source, check_invoice_logo(_read_original(store, source), source.identity)
        )
    except MoneyError as error:
        raise LedgerError(error.code, 503, "The Invoice Logo could not be verified.") from None


class InvoiceIssuanceService:
    def __init__(self, engine, *, store, font_directory):
        self.reservations = InvoiceReservationCommand(engine)
        self.sources = InvoiceRenderSources(engine)
        self.files = InvoiceOriginalFiles(engine)
        self.finalization = InvoiceFinalization(engine)
        self.store = store
        self.font_directory = font_directory

    async def issue(self, owner, ledger, command, key, *, raw_body=None):
        # Detach caller-owned Pydantic objects and raw bytes before the first await.
        if not isinstance(command, InvoiceIssuanceCreate) or (
            raw_body is not None and type(raw_body) is not bytes
        ):
            raise LedgerError("invalid_request", 422, "The Invoice intent must be valid.")
        try:
            command = InvoiceIssuanceCreate.model_validate(command.model_dump())
        except ValueError:
            raise LedgerError("invalid_request", 422, "The Invoice intent must be valid.") from None
        run = anyio.to_thread.run_sync
        reserve = partial(self.reservations.reserve, owner, ledger, command, key, raw_body=raw_body)
        first = await run(reserve)
        if isinstance(first, FreshInvoiceLogo):
            # The reservation transaction is closed while reading and decoding private bytes.
            checked = await run(partial(_check_logo, self.store, first))
            first = await run(partial(reserve, logo_check=checked))
        if not isinstance(first, (InvoiceIssuancePending, InvoiceIssuanceComplete)):
            raise _integrity()
        try:
            identity = InvoiceIssuanceIdentity.model_validate(
                first.model_dump(include=set(InvoiceIssuanceIdentity.model_fields))
            )
        except (ValueError, AttributeError, TypeError):
            raise _integrity() from None
        if (
            identity.id,
            identity.ledger_id,
            identity.document_id,
            identity.document_version,
            identity.configuration_id,
            identity.configuration_version,
            identity.previous_issuance_id,
        ) != (
            command.id,
            ledger,
            command.document_id,
            command.expected_document_version,
            command.configuration_id,
            command.expected_configuration_version,
            command.previous_issuance_id,
        ):
            raise _integrity()
        observed = _receipt(first, identity)
        if isinstance(observed, InvoiceIssuanceComplete):
            return observed
        source = await run(partial(self.sources.load, owner, ledger, command.id, store=self.store))
        if (source.ledger_id, source.issuance_id, source.snapshot.sha256) != (
            ledger,
            command.id,
            identity.snapshot_sha256,
        ):
            raise _integrity()
        try:
            prepared = await prepare_invoice_blobs(
                source.snapshot.content,
                expected_sha256=identity.snapshot_sha256,
                font_directory=self.font_directory,
                store=self.store,
                logo_content=source.logo_content,
            )
        except MoneyError as error:
            raise LedgerError(
                error.code, 503, "Both private Invoice originals could not be prepared."
            ) from None
        verified = await run(
            partial(
                self.files.select_verified, owner, ledger, command.id, prepared, store=self.store
            )
        )
        result = _receipt(
            await run(
                partial(
                    self.finalization.seal,
                    owner,
                    ledger,
                    command.id,
                    prepared=prepared,
                    verified=verified,
                )
            ),
            identity,
        )
        if not isinstance(result, InvoiceIssuanceComplete):
            raise _integrity()
        return result
