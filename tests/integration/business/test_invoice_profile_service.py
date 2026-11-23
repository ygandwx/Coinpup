"""Real company master-data writes, conflicts, Logo review and repeatable scoped reads."""

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_issuance_schemas import (
    InvoiceProfileArchive,
    InvoiceProfileCreate,
    InvoiceProfileFields,
    InvoiceProfileReplacement,
)
from coinpup_api.business.invoice_profiles import InvoiceProfileService
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.models import Entity
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.models import ChangeLog
from pydantic import ValidationError
from sqlalchemy import select, update

from tests.integration.business.test_invoice_profile_storage import file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def profiles(setup):
    s = setup
    s["profiles"] = InvoiceProfileService(s["engine"])
    s["request"] = InvoiceProfileCreate(
        id=s["profile"]["id"],
        name="Fictional Invoice 配置",
        numbering={"prefix": "虚构-", "minimum_digits": 20},
        footer="Fictional footer",
        payment_instructions="Fictional instructions",
    )
    return s


def create(s, **changes):
    request = InvoiceProfileCreate.model_validate(s["request"].model_dump() | changes)
    return s["profiles"].create(s["owner"], s["profile_ledger"], request)


def replacement(row, **changes):
    return InvoiceProfileReplacement.model_validate(
        row.model_dump(include=set(InvoiceProfileFields.model_fields))
        | {"expected_version": row.version}
        | changes
    )


def test_complete_configuration_replacement_archive_and_reads_only_change_master_data(profiles):
    s = profiles
    before = snapshot(s)
    row = create(s)
    assert row.version == 1 and row.numbering.prefix == "虚构-"
    changed = s["profiles"].replace(
        s["owner"],
        s["profile_ledger"],
        row.id,
        InvoiceProfileReplacement(
            name="Fictional replacement",
            expected_version=1,
            numbering={"prefix": "F-", "minimum_digits": 1},
        ),
    )
    assert changed.version == 2 and changed.footer is None and changed.payment_instructions is None
    assert changed.updated_at >= row.updated_at and changed.created_at == row.created_at
    for version, archived in [(2, True), (3, False)]:
        current = s["profiles"].archive(
            s["owner"],
            s["profile_ledger"],
            row.id,
            InvoiceProfileArchive(expected_version=version, archived=archived),
        )
        assert current.archived is archived and current.version == version + 1
        saved = snapshot(s)
        assert s["profiles"].get(s["owner"], s["profile_ledger"], row.id) == current
        assert s["profiles"].list(s["owner"], s["profile_ledger"]) == [current]
        assert s["profiles"].list(s["owner"], s["profile_ledger"], include_archived=False) == (
            [] if archived else [current]
        )
        assert snapshot(s) == saved
    after = snapshot(s)
    ignored = {"business_invoice_profiles", "change_log"}
    assert {k: v for k, v in before.items() if k not in ignored} == {
        k: v for k, v in after.items() if k not in ignored
    }
    with s["engine"].connect() as connection:
        assert connection.execute(
            select(ChangeLog.entity_version, ChangeLog.change_kind)
            .where(ChangeLog.entity_type == "business_invoice_profiles")
            .order_by(ChangeLog.seq)
        ).all() == [(1, "upsert"), (2, "upsert"), (3, "archive"), (4, "restore")]


@pytest.mark.parametrize("method", ["create", "replace", "archive"])
def test_unknown_write_retries_conflict_without_repeating_changes(profiles, method):
    s = profiles
    original = create(s)
    if method == "create":
        invoke = partial(create, s)
        code = "duplicate_record"
    elif method == "replace":
        command = replacement(original, name="Fictional updated name")
        invoke = partial(
            s["profiles"].replace, s["owner"], s["profile_ledger"], original.id, command
        )
        invoke()
        code = "version_conflict"
    else:
        command = InvoiceProfileArchive(expected_version=1, archived=True)
        invoke = partial(
            s["profiles"].archive, s["owner"], s["profile_ledger"], original.id, command
        )
        invoke()
        code = "version_conflict"
    saved = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        invoke()
    assert failure.value.code == code and snapshot(s) == saved


@pytest.mark.parametrize("create_new", [False, True])
def test_concurrent_reviewed_intentions_commit_only_once(profiles, create_new):
    s = profiles
    if not create_new:
        row = create(s)
        request = replacement(row, footer="Fictional concurrent change")
    gate = Barrier(2)

    def invoke(_):
        gate.wait()
        try:
            return (
                create(s)
                if create_new
                else s["profiles"].replace(s["owner"], s["profile_ledger"], row.id, request)
            )
        except LedgerError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    failures = [r for r in results if isinstance(r, LedgerError)]
    assert len(failures) == 1
    assert failures[0].code == ("duplicate_record" if create_new else "version_conflict")
    assert s["profiles"].get(s["owner"], s["profile_ledger"], s["request"].id).version == (
        1 if create_new else 2
    )


@pytest.mark.parametrize("method", ["get", "list", "replace", "archive", "create"])
@pytest.mark.parametrize("fault", ["owner", "ledger", "personal"])
def test_company_scope_is_required_and_failure_writes_nothing(profiles, method, fault):
    s = profiles
    row = create(s)
    # This deployment has one DB-enforced administrator; another identity is absent.
    owner = uuid4() if fault == "owner" else s["owner"]
    ledger = (
        s["ledger"]
        if fault == "personal"
        else uuid4()
        if fault == "ledger"
        else s["profile_ledger"]
    )
    args = [owner, ledger]
    if method in {"get", "replace", "archive"}:
        args.append(row.id)
    if method == "replace":
        args.append(replacement(row))
    elif method == "archive":
        args.append(InvoiceProfileArchive(expected_version=1, archived=True))
    elif method == "create":
        args.append(s["request"])
    saved = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        getattr(s["profiles"], method)(*args)
    assert failure.value.code == (
        "invoice_requires_company" if fault == "personal" else "not_found"
    )
    assert snapshot(s) == saved


def test_another_owned_company_has_independent_list_and_cannot_edit_the_other_record(profiles):
    s = profiles
    row = create(s)
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="company",
                name="Fictional other issuer",
                country_code="HK",
                company_type="private_limited",
                base_asset_id="USD",
            ),
        )
        .ledger.id
    )
    saved = snapshot(s)
    assert s["profiles"].list(s["owner"], other) == []
    with pytest.raises(LedgerError) as failure:
        s["profiles"].replace(s["owner"], other, row.id, replacement(row))
    assert failure.value.code == "not_found" and snapshot(s) == saved


@pytest.mark.parametrize(
    "fault,code",
    [
        ("ledger", "not_found"),
        ("stale", "version_conflict"),
        ("archived", "file_archived"),
        ("pdf", "invalid_invoice_logo"),
    ],
)
def test_selecting_a_logo_checks_scope_current_review_and_supported_image(profiles, fault, code):
    s = profiles
    identifier = file(
        s,
        **(
            {"ledger_id": s["ledger"]}
            if fault == "ledger"
            else {"detected_media_type": "application/pdf"}
            if fault == "pdf"
            else {}
        ),
    )
    if fault == "archived":
        with s["engine"].begin() as connection:
            connection.execute(
                update(StoredFile)
                .where(StoredFile.id == identifier)
                .values(archived=True, version=2)
            )
    saved = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        create(
            s, logo_file_id=identifier, logo_file_version=2 if fault in {"stale", "archived"} else 1
        )
    assert failure.value.code == code and snapshot(s) == saved


def test_unchanged_reviewed_logo_survives_metadata_changes_but_new_selection_requires_current(
    profiles,
):
    s = profiles
    identifier = file(s)
    row = create(s, logo_file_id=identifier, logo_file_version=1)
    with s["engine"].begin() as connection:
        connection.execute(
            update(StoredFile)
            .where(StoredFile.id == identifier)
            .values(title="Edited title", version=2)
        )
    changed = s["profiles"].replace(
        s["owner"], s["profile_ledger"], row.id, replacement(row, footer="Edited footer")
    )
    assert changed.logo_file_version == 1
    reviewed = s["profiles"].replace(
        s["owner"], s["profile_ledger"], row.id, replacement(changed, logo_file_version=2)
    )
    assert reviewed.logo_file_version == 2 and reviewed.logo_file_id == identifier


def test_archived_company_remains_readable_and_rejects_new_configuration_writes(profiles):
    s = profiles
    row = create(s)
    with s["engine"].begin() as connection:
        connection.execute(
            update(Entity).where(Entity.kind == "company").values(archived=True, version=2)
        )
    saved = snapshot(s)
    assert s["profiles"].get(s["owner"], s["profile_ledger"], row.id) == row
    with pytest.raises(LedgerError) as failure:
        s["profiles"].archive(
            s["owner"],
            s["profile_ledger"],
            row.id,
            InvoiceProfileArchive(expected_version=1, archived=True),
        )
    assert failure.value.code == "entity_archived" and snapshot(s) == saved


def test_internal_copy_bypass_is_revalidated_before_a_write(profiles):
    s = profiles
    saved = snapshot(s)
    invalid = s["request"].model_copy(update={"logo_file_id": uuid4()})
    with pytest.raises(ValidationError):
        s["profiles"].create(s["owner"], s["profile_ledger"], invalid)
    assert snapshot(s) == saved


@pytest.mark.parametrize(
    "page",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": True},
        {"offset": -1},
        {"offset": 100001},
    ],
)
def test_invalid_pagination_never_reads_a_partial_unbounded_catalog(profiles, page):
    s = profiles
    saved = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        s["profiles"].list(s["owner"], s["profile_ledger"], **page)
    assert failure.value.code == "invalid_pagination" and snapshot(s) == saved


def test_pages_are_stable_and_archived_filter_does_not_hide_other_configurations(profiles):
    s = profiles
    first, second = create(s), create(s, id=uuid4())
    args = (s["owner"], s["profile_ledger"])
    assert s["profiles"].list(*args, limit=1, offset=0) == [first]
    assert s["profiles"].list(*args, limit=1, offset=1) == [second]
    s["profiles"].archive(*args, first.id, InvoiceProfileArchive(expected_version=1, archived=True))
    assert s["profiles"].list(*args, include_archived=False) == [second]


@pytest.mark.parametrize("method", ["create", "replace", "archive"])
def test_late_response_failure_rolls_back_configuration_and_its_notification(
    profiles, monkeypatch, method
):
    from coinpup_api.business import invoice_profiles

    s = profiles
    row = create(s) if method != "create" else None
    saved = snapshot(s)

    def fail(_):
        raise RuntimeError("Fictional failure after flush before commit")

    monkeypatch.setattr(invoice_profiles, "profile_response", fail)
    with pytest.raises(RuntimeError, match="Fictional failure"):
        if method == "create":
            create(s)
        elif method == "replace":
            s["profiles"].replace(
                s["owner"], s["profile_ledger"], row.id, replacement(row, name="Changed")
            )
        else:
            s["profiles"].archive(
                s["owner"],
                s["profile_ledger"],
                row.id,
                InvoiceProfileArchive(expected_version=1, archived=True),
            )
    assert snapshot(s) == saved
