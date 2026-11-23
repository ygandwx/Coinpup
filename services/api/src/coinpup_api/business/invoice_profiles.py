"""Company configuration uses the existing scoped master-data version protocol."""

from sqlalchemy import select

from coinpup_api.business.invoice_issuance_schemas import (
    InvoiceProfileArchive,
    InvoiceProfileCreate,
    InvoiceProfileFields,
    InvoiceProfileReplacement,
)
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.schemas import BusinessIdentityResponse
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.service import (
    LedgerError,
    LedgerService,
    _not_found,
    _page,
    _touch,
    _version,
)


class InvoiceProfileResponse(InvoiceProfileFields, BusinessIdentityResponse):
    """Current configuration only; it does not assert any number was issued."""


def profile_response(row):
    return InvoiceProfileResponse(
        id=row.id,
        ledger_id=row.ledger_id,
        version=row.version,
        archived=row.archived,
        created_at=row.created_at,
        updated_at=row.updated_at,
        name=row.name,
        numbering={"prefix": row.number_prefix, "minimum_digits": row.minimum_digits},
        issuer_extra_text=row.issuer_extra_text,
        payment_instructions=row.payment_instructions,
        footer=row.footer,
        logo_file_id=row.logo_file_id,
        logo_file_version=row.logo_file_version,
    )


def _values(payload):
    values = payload.model_dump(exclude={"id", "expected_version"})
    numbering = values.pop("numbering")
    return values | dict(
        number_prefix=numbering["prefix"], minimum_digits=numbering["minimum_digits"]
    )


class InvoiceProfileService(LedgerService):
    def _company(self, session, owner, ledger, *, write=False):
        _, entity = self._ledger(session, owner, ledger, write=write)
        if entity.kind != "company":
            raise LedgerError(
                "invoice_requires_company", 422, "Invoice configuration requires a company."
            )

    @staticmethod
    def _record(session, ledger, identifier):
        row = session.scalar(
            select(InvoiceProfile).where(
                InvoiceProfile.id == identifier, InvoiceProfile.ledger_id == ledger
            )
        )
        if row is None:
            raise _not_found()
        return row

    @staticmethod
    def _logo(session, ledger, payload, previous=None):
        reference = (payload.logo_file_id, payload.logo_file_version)
        if reference == (None, None) or (
            previous is not None
            and reference == (previous.logo_file_id, previous.logo_file_version)
        ):
            # An unchanged retained reference identifies the original immutable content,
            # even if its editable title or archival state has advanced since review.
            return
        file = session.scalar(
            select(StoredFile)
            .where(StoredFile.id == payload.logo_file_id, StoredFile.ledger_id == ledger)
            .with_for_update(read=True)
        )
        if file is None:
            raise _not_found()
        _version(file, payload.logo_file_version)
        if file.archived:
            raise LedgerError("file_archived", 409, "Restore the file before selecting it.")
        if file.detected_media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise LedgerError("invalid_invoice_logo", 422, "Choose a supported private image.")

    def create(self, owner, ledger, payload: InvoiceProfileCreate):
        payload = InvoiceProfileCreate.model_validate(payload.model_dump())
        with self._transaction(owner, write=True) as session:
            self._company(session, owner, ledger, write=True)
            self._logo(session, ledger, payload)
            row = InvoiceProfile(id=payload.id, ledger_id=ledger, **_values(payload))
            session.add(row)
            session.flush()
            return profile_response(row)

    def replace(self, owner, ledger, identifier, payload: InvoiceProfileReplacement):
        payload = InvoiceProfileReplacement.model_validate(payload.model_dump())
        with self._transaction(owner, write=True) as session:
            self._company(session, owner, ledger, write=True)
            row = self._record(session, ledger, identifier)
            _version(row, payload.expected_version)
            self._logo(session, ledger, payload, row)
            for field, value in _values(payload).items():
                setattr(row, field, value)
            _touch(row)
            session.flush()
            session.refresh(row)
            return profile_response(row)

    def archive(self, owner, ledger, identifier, payload: InvoiceProfileArchive):
        payload = InvoiceProfileArchive.model_validate(payload.model_dump())
        with self._transaction(owner, write=True) as session:
            self._company(session, owner, ledger, write=True)
            row = self._record(session, ledger, identifier)
            _version(row, payload.expected_version)
            row.archived = payload.archived
            _touch(row)
            session.flush()
            session.refresh(row)
            return profile_response(row)

    def get(self, owner, ledger, identifier):
        with self._transaction(owner, read_only=True) as session:
            self._company(session, owner, ledger)
            return profile_response(self._record(session, ledger, identifier))

    def list(self, owner, ledger, *, include_archived=True, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner, read_only=True) as session:
            self._company(session, owner, ledger)
            statement = select(InvoiceProfile).where(InvoiceProfile.ledger_id == ledger)
            if not include_archived:
                statement = statement.where(InvoiceProfile.archived.is_(False))
            rows = session.scalars(
                statement.order_by(InvoiceProfile.created_at, InvoiceProfile.id)
                .limit(limit)
                .offset(offset)
            )
            return [profile_response(row) for row in rows]
