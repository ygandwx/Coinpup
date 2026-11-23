"""Fictional current/archived configuration and retained private Logo in a full bundle."""

from uuid import uuid4

from _database_archive import ArchiveError
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.files.models import StoredFile
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, update


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


def seed_invoice_profiles(engine, ledger, logo):
    first, second = uuid4(), uuid4()
    with engine.begin() as connection:
        for identifier, prefix in [(first, "虚构-"), (second, "FICTIONAL-")]:
            connection.execute(
                insert(InvoiceProfile).values(
                    id=identifier,
                    ledger_id=ledger,
                    name="Fictional Invoice 公司配置",
                    number_prefix=prefix,
                    minimum_digits=20,
                    issuer_extra_text="Fictional issuer",
                    payment_instructions="Fictional bank",
                    footer="Fictional commercial Invoice",
                    logo_file_id=logo,
                    logo_file_version=1,
                )
            )
        connection.execute(
            update(InvoiceProfile)
            .where(InvoiceProfile.id == first)
            .values(archived=True, version=2)
        )
        # Metadata edits cannot silently move the originally reviewed immutable Logo identity.
        connection.execute(
            update(StoredFile)
            .where(StoredFile.id == logo)
            .values(title="Changed Logo title", version=2)
        )
    return observations(engine)


def verify_invoice_profiles(engine, expected):
    if observations(engine) != expected:
        raise ArchiveError(
            "Restored Invoice configuration, Logo reference or notifications changed."
        )
