"""Company configuration retains scoped Logo references and continuous version notifications."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_profile_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(ledger_setup):
    s = ledger_setup
    company = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="company",
            name="Fictional Invoice company",
            country_code="HK",
            company_type="private_limited",
            base_asset_id="USD",
        ),
    )
    s["profile_ledger"] = company.ledger.id
    s["profile"] = dict(
        id=uuid4(),
        ledger_id=company.ledger.id,
        name="Fictional bilingual configuration",
        number_prefix="虚构-",
        minimum_digits=6,
        issuer_extra_text="Fictional 公司",
        payment_instructions="Fictional payment instructions",
        footer="Fictional commercial Invoice",
    )
    return s


def file(s, **changes):
    values = (
        dict(
            id=uuid4(),
            ledger_id=s["profile_ledger"],
            created_by=s["owner"],
            blob_key=uuid4().hex,
            sha256="a" * 64,
            byte_size=12,
            detected_media_type="image/png",
            original_filename="fictional-logo.png",
            title="Fictional Logo",
        )
        | changes
    )
    with s["engine"].begin() as c:
        c.execute(insert(StoredFile).values(**values))
    return values["id"]


def add(s, **changes):
    with s["engine"].begin() as c:
        c.execute(insert(InvoiceProfile).values(**(s["profile"] | changes)))


def test_configuration_versions_and_archive_notify_the_same_owned_identity_only(setup):
    s = setup
    before = snapshot(s)
    add(s)
    with s["engine"].begin() as c:
        for version, values in [
            (2, dict(number_prefix="FICTIONAL-", minimum_digits=20)),
            (3, dict(archived=True)),
            (4, dict(archived=False)),
        ]:
            c.execute(
                update(InvoiceProfile)
                .where(InvoiceProfile.id == s["profile"]["id"])
                .values(version=version, **values)
            )
        changes = c.execute(
            select(
                ChangeLog.owner_id,
                ChangeLog.ledger_id,
                ChangeLog.entity_version,
                ChangeLog.change_kind,
            )
            .where(ChangeLog.entity_type == InvoiceProfile.__tablename__)
            .order_by(ChangeLog.seq)
        ).all()
        assert changes == [
            (s["owner"], s["profile_ledger"], n, kind)
            for n, kind in [(1, "upsert"), (2, "upsert"), (3, "archive"), (4, "restore")]
        ]
    after = snapshot(s)
    assert {
        k: v for k, v in after.items() if k not in {"business_invoice_profiles", "change_log"}
    } == {k: v for k, v in before.items() if k not in {"business_invoice_profiles", "change_log"}}


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"archived": True},
        {"name": " "},
        {"number_prefix": " leading"},
        {"number_prefix": "bad\n"},
        {"minimum_digits": 0},
        {"minimum_digits": 21},
        {"logo_file_version": 1},
        {"logo_file_id": uuid4()},
    ],
)
def test_invalid_configuration_rolls_back_record_and_notification(setup, changes):
    s = setup
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        add(s, **changes)
    assert snapshot(s) == before


def test_personal_ledger_cannot_hold_a_company_invoice_configuration(setup):
    s = setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught:
        add(s, ledger_id=s["ledger"])
    assert caught.value.orig.diag.constraint_name == "ck_invoice_profile_company"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["skip_version", "same_version", "identity", "ledger", "created_at", "delete"]
)
def test_stable_identity_and_continuous_versions_cannot_be_overwritten(setup, fault):
    s = setup
    add(s)
    before = snapshot(s)
    changes = {"version": 2}
    if fault == "skip_version":
        changes["version"] = 3
    elif fault == "same_version":
        changes["version"] = 1
    elif fault == "identity":
        changes["id"] = uuid4()
    elif fault == "ledger":
        changes["ledger_id"] = s["ledger"]
    elif fault == "created_at":
        changes["created_at"] = text("created_at + interval '1 day'")
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        statement = (
            delete(InvoiceProfile)
            if fault == "delete"
            else update(InvoiceProfile).values(**changes)
        )
        c.execute(statement.where(InvoiceProfile.id == s["profile"]["id"]))
    assert caught.value.orig.diag.constraint_name == "ck_invoice_profile_identity"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["other_ledger", "version", "pdf", "archived"])
def test_new_logo_selection_requires_reviewed_private_image_in_same_ledger(setup, fault):
    s = setup
    identifier = file(
        s,
        **(
            {"ledger_id": s["ledger"]}
            if fault == "other_ledger"
            else {"detected_media_type": "application/pdf"}
            if fault == "pdf"
            else {}
        ),
    )
    if fault == "archived":
        with s["engine"].begin() as c:
            c.execute(
                update(StoredFile)
                .where(StoredFile.id == identifier)
                .values(archived=True, version=2)
            )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught:
        add(
            s,
            logo_file_id=identifier,
            logo_file_version=2 if fault in {"version", "archived"} else 1,
        )
    assert caught.value.orig.diag.constraint_name == "ck_invoice_profile_logo_scope"
    assert snapshot(s) == before


def test_existing_logo_keeps_immutable_content_identity_after_metadata_change(setup):
    s = setup
    identifier = file(s)
    add(s, logo_file_id=identifier, logo_file_version=1)
    with s["engine"].begin() as c:
        c.execute(
            update(StoredFile)
            .where(StoredFile.id == identifier)
            .values(title="Changed fictional title", version=2)
        )
        c.execute(
            update(InvoiceProfile)
            .where(InvoiceProfile.id == s["profile"]["id"])
            .values(name="Changed fictional name", version=2)
        )
        assert c.execute(
            select(InvoiceProfile.logo_file_id, InvoiceProfile.logo_file_version)
        ).one() == (identifier, 1)
        assert c.execute(select(StoredFile.sha256, StoredFile.byte_size)).one() == ("a" * 64, 12)


def test_empty_round_trip_preserves_whole_catalog_and_existing_rows(setup):
    s = setup
    before = snapshot(s)
    migration = invoice_profile_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        migration["downgrade"]()
        assert (
            c.exec_driver_sql("SELECT to_regclass('public.business_invoice_profiles')").scalar()
            is None
        )
        migration["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(s) == before


@pytest.mark.parametrize("history_only", [False, True])
def test_configuration_or_notification_history_blocks_downgrade(setup, history_only):
    s = setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        if history_only:
            c.exec_driver_sql(
                "CREATE TEMP TABLE business_invoice_profiles(id uuid,ledger_id uuid,version int) "
                "ON COMMIT DROP"
            )
            c.exec_driver_sql(
                "CREATE TRIGGER fictional_profile_change AFTER INSERT "
                "ON pg_temp.business_invoice_profiles FOR EACH ROW "
                "EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_invoice_profiles VALUES (:id,:ledger,1)"),
                {"id": uuid4(), "ledger": s["profile_ledger"]},
            )
        else:
            c.execute(insert(InvoiceProfile).values(**s["profile"]))
        with Operations.context(MigrationContext.configure(c)):
            invoice_profile_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_profile_downgrade"
    assert snapshot(s) == before
