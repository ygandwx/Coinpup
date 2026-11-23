"""Fictional current/archived configuration and retained private Logo in a full bundle."""

from uuid import uuid4

from _database_archive import ArchiveError
from coinpup_api.business.invoice_issuance_schemas import (
    InvoiceProfileArchive,
    InvoiceProfileCreate,
)
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_profiles import InvoiceProfileService
from coinpup_api.files.models import StoredFile
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import select, update


def observations(engine):
    with engine.connect() as connection:
        return (
            tuple(connection.execute(select(InvoiceProfile.__table__).order_by(InvoiceProfile.id))),
            tuple(
                connection.execute(
                    select(ChangeLog.__table__)
                    .where(ChangeLog.entity_type == "business_invoice_profiles")
                    .order_by(ChangeLog.seq)
                )
            ),
        )


def seed_invoice_profiles(engine, owner, ledger, logo):
    first, second = uuid4(), uuid4()
    service = InvoiceProfileService(engine)
    for identifier, prefix in [(first, "虚构-"), (second, "FICTIONAL-")]:
        service.create(
            owner,
            ledger,
            InvoiceProfileCreate(
                id=identifier,
                name="Fictional Invoice 公司配置",
                numbering={"prefix": prefix, "minimum_digits": 20},
                issuer_extra_text="Fictional issuer",
                payment_instructions="Fictional bank",
                footer="Fictional commercial Invoice",
                logo_file_id=logo,
                logo_file_version=1,
            ),
        )
    service.archive(owner, ledger, first, InvoiceProfileArchive(expected_version=1, archived=True))
    with engine.begin() as connection:
        # Metadata edits cannot silently move the originally reviewed immutable Logo identity.
        connection.execute(
            update(StoredFile)
            .where(StoredFile.id == logo)
            .values(title="Changed Logo title", version=2)
        )
    return dict(
        owner=owner,
        ledger=ledger,
        expected=observations(engine),
        current=tuple(service.get(owner, ledger, i) for i in (first, second)),
    )


def verify_invoice_profiles(engine, case):
    service = InvoiceProfileService(engine)
    actual = tuple(service.get(case["owner"], case["ledger"], row.id) for row in case["current"])
    if actual != case["current"] or observations(engine) != case["expected"]:
        raise ArchiveError(
            "Restored Invoice configuration, Logo reference or notifications changed."
        )
    active = service.list(case["owner"], case["ledger"], include_archived=False)
    if active != [row for row in case["current"] if not row.archived]:
        raise ArchiveError("Restored active Invoice configurations changed.")
