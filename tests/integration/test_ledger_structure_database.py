"""Real PostgreSQL transactions, isolation and optimistic concurrency for ledger structure."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import Account, AccountAsset, Category, Entity, Ledger
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetCreate,
    AssetUpdate,
    CategoryCreate,
    CategoryUpdate,
    EntityCreate,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.ledger.templates import TEMPLATES
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


def personal(service, owner, name="Fictional personal", **kwargs):
    return service.create_entity(
        owner, EntityCreate(kind="personal", name=name, base_asset_id="USD", **kwargs)
    )


def test_entity_and_primary_ledger_creation_is_atomic_with_stable_ids(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity_id, ledger_id = uuid4(), uuid4()
    created = personal(service, owner, id=entity_id, ledger_id=ledger_id)
    assert (created.id, created.ledger.id, created.ledger.entity_id) == (
        entity_id,
        ledger_id,
        entity_id,
    )
    assert created.version == created.ledger.version == 1
    assert service.get_ledger(owner, ledger_id) == created.ledger
    # A ledger ID collision occurs after entity insertion; the complete command rolls back.
    failed_entity = uuid4()
    with pytest.raises(LedgerError) as error:
        personal(service, owner, id=failed_entity, ledger_id=ledger_id)
    assert (error.value.code, error.value.status) == ("duplicate_record", 409)
    with Session(engine) as session:
        assert session.get(Entity, failed_entity) is None
        assert session.scalar(select(func.count()).select_from(Ledger)) == 1
    with pytest.raises(LedgerError) as repeated:
        personal(service, owner, id=entity_id)
    assert repeated.value.code == "duplicate_record"


def test_templates_are_copied_to_independent_ledger_categories(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    one = personal(service, owner, template_key="business_default")
    two = personal(service, owner, template_key="business_default", locale="en")
    first = service.list_categories(owner, one.ledger.id)
    second = service.list_categories(owner, two.ledger.id)
    assert len(first) == len(second) == len(TEMPLATES["business_default"].categories)
    assert not {item.id for item in first} & {item.id for item in second}
    matching = next(item for item in second if item.template_key == first[0].template_key)
    old_name = matching.name
    changed = service.update_category(
        owner,
        one.ledger.id,
        first[0].id,
        CategoryUpdate(expected_version=1, name="独立修改", name_en=None),
    )
    assert changed.name == "独立修改" and changed.name_en is None and changed.version == 2
    matching = next(
        item for item in service.list_categories(owner, two.ledger.id) if item.id == matching.id
    )
    assert matching.name == matching.name_en == old_name
    assert service.list_templates(owner) == list(TEMPLATES.values())


def test_company_profile_update_validates_the_merged_record(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity = service.create_entity(
        owner,
        EntityCreate(
            kind="company",
            name="Fictional LLC",
            country_code="US",
            region_code="NM",
            company_type="llc",
            base_asset_id="USD",
            registration_date="2026-01-01",
        ),
    )
    with pytest.raises(LedgerError) as error:
        service.update_entity(owner, entity.id, EntityUpdate(expected_version=1, country_code="HK"))
    assert error.value.code == "invalid_profile"
    unchanged = service.get_entity(owner, entity.id)
    assert unchanged.version == 1 and unchanged.country_code == "US"
    updated = service.update_entity(
        owner,
        entity.id,
        EntityUpdate(
            expected_version=1,
            country_code="HK",
            region_code=None,
            company_type="private_limited",
            details={"business_registration_number": "FICTIONAL"},
        ),
    )
    assert updated.country_code == "HK" and updated.region_code is None and updated.version == 2
    assert updated.ledger == entity.ledger


def test_assets_are_explicit_and_disabled_assets_remain_readable(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    jpy = service.create_asset(owner, AssetCreate(code="JPY", kind="fiat", scale=0))
    token = service.create_asset(
        owner,
        AssetCreate(
            code="USDC",
            kind="token",
            scale=6,
            network="fictional-chain",
            token_reference="FictionalToken",
        ),
    )
    assert token.asset_id == "token:fictional-chain:FictionalToken"
    with pytest.raises(LedgerError) as duplicate:
        service.create_asset(owner, AssetCreate(code="JPY", kind="fiat", scale=2))
    assert duplicate.value.code == "duplicate_record"
    with pytest.raises(LedgerError) as invalid:
        service.create_asset(owner, AssetCreate(code="USD", kind="fiat", scale=0))
    assert invalid.value.code == "asset_definition"
    entity = personal(service, owner)
    account = service.create_account(
        owner,
        entity.ledger.id,
        AccountCreate(
            name="Fictional wallet", kind="wise", asset_ids=["USD", "JPY", token.asset_id]
        ),
    )
    disabled = service.update_asset(
        owner, "JPY", AssetUpdate(expected_version=jpy.version, enabled=False)
    )
    assert disabled.version == 2
    assert "JPY" in service.list_accounts(owner, entity.ledger.id)[0].asset_ids
    assert "JPY" not in {
        item.asset_id for item in service.list_assets(owner, include_disabled=False)
    }
    assert "JPY" in {item.asset_id for item in service.list_assets(owner)}
    with pytest.raises(LedgerError) as rejected:
        service.create_account(
            owner, entity.ledger.id, AccountCreate(name="Rejected", kind="cash", asset_ids=["JPY"])
        )
    assert rejected.value.code == "asset_disabled"
    assert [item.id for item in service.list_accounts(owner, entity.ledger.id)] == [account.id]
    with pytest.raises(LedgerError) as stale:
        service.update_asset(owner, "JPY", AssetUpdate(expected_version=1, enabled=True))
    assert stale.value.code == "version_conflict"


def test_base_currency_is_enabled_fiat_and_invalid_creation_leaves_no_entity(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    for asset_id, code in [("BTC", "invalid_base_asset"), ("missing", "not_found")]:
        with pytest.raises(LedgerError) as error:
            service.create_entity(
                owner, EntityCreate(kind="personal", name="Rejected", base_asset_id=asset_id)
            )
        assert error.value.code == code
    service.update_asset(owner, "USD", AssetUpdate(expected_version=1, enabled=False))
    with pytest.raises(LedgerError) as error:
        personal(service, owner)
    assert error.value.code == "asset_disabled"
    assert service.list_entities(owner) == []


def test_account_assets_change_without_deleting_historical_links(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity = personal(service, owner)
    account = service.create_account(
        owner,
        entity.ledger.id,
        AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD", "EUR"]),
    )
    updated = service.update_account(
        owner,
        entity.ledger.id,
        account.id,
        AccountUpdate(expected_version=1, asset_ids=["USD", "BTC"], details={"note": "fictional"}),
    )
    assert updated.asset_ids == ["BTC", "USD"] and updated.version == 2
    with Session(engine) as session:
        links = session.scalars(
            select(AccountAsset).where(AccountAsset.account_id == account.id)
        ).all()
        assert {link.asset_id: link.enabled for link in links} == {
            "BTC": True,
            "EUR": False,
            "USD": True,
        }
    restored = service.update_account(
        owner,
        entity.ledger.id,
        account.id,
        AccountUpdate(expected_version=2, asset_ids=["EUR", "USD"]),
    )
    assert restored.asset_ids == ["EUR", "USD"]
    archived = service.update_account(
        owner, entity.ledger.id, account.id, AccountUpdate(expected_version=3, archived=True)
    )
    assert archived.version == 4
    assert service.list_accounts(owner, entity.ledger.id) == []
    assert service.list_accounts(owner, entity.ledger.id, include_archived=True)[0].id == account.id
    with Session(engine) as session:
        assert session.get(Account, account.id) is not None


def test_cross_ledger_references_and_unknown_owners_are_indistinguishable_from_missing(
    structure_database,
):
    engine, owner = structure_database
    service = LedgerService(engine)
    one, two = personal(service, owner), personal(service, owner)
    account = service.create_account(
        owner, one.ledger.id, AccountCreate(name="Fictional cash", kind="cash", asset_ids=["USD"])
    )
    category = service.create_category(
        owner, one.ledger.id, CategoryCreate(name="Parent", kind="expense")
    )
    operations = [
        lambda: service.update_account(
            owner, two.ledger.id, account.id, AccountUpdate(expected_version=1, name="Bad")
        ),
        lambda: service.update_category(
            owner, two.ledger.id, category.id, CategoryUpdate(expected_version=1, name="Bad")
        ),
        lambda: service.create_category(
            owner, two.ledger.id, CategoryCreate(name="Bad", kind="expense", parent_id=category.id)
        ),
        lambda: service.get_entity(uuid4(), one.id),
        lambda: service.get_ledger(uuid4(), one.ledger.id),
        lambda: service.list_accounts(uuid4(), one.ledger.id),
        lambda: service.list_categories(uuid4(), one.ledger.id),
    ]
    for operation in operations:
        with pytest.raises(LedgerError) as error:
            operation()
        assert (error.value.code, error.value.status, str(error.value)) == (
            "not_found",
            404,
            "The requested record was not found.",
        )


def test_category_archive_hierarchy_and_parent_kind_guards(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity = personal(service, owner)
    ledger_id = entity.ledger.id
    parent = service.create_category(
        owner, ledger_id, CategoryCreate(name="Parent", kind="expense")
    )
    child = service.create_category(
        owner, ledger_id, CategoryCreate(name="Child", kind="expense", parent_id=parent.id)
    )
    with pytest.raises(LedgerError) as mismatch:
        service.create_category(
            owner, ledger_id, CategoryCreate(name="Bad", kind="income", parent_id=parent.id)
        )
    assert mismatch.value.code == "category_kind_mismatch"
    with pytest.raises(LedgerError) as active:
        service.update_category(
            owner, ledger_id, parent.id, CategoryUpdate(expected_version=1, archived=True)
        )
    assert active.value.code == "active_children"
    service.update_category(
        owner, ledger_id, child.id, CategoryUpdate(expected_version=1, archived=True)
    )
    service.update_category(
        owner, ledger_id, parent.id, CategoryUpdate(expected_version=1, archived=True)
    )
    for operation in [
        lambda: service.create_category(
            owner, ledger_id, CategoryCreate(name="Bad", kind="expense", parent_id=parent.id)
        ),
        lambda: service.update_category(
            owner, ledger_id, child.id, CategoryUpdate(expected_version=2, archived=False)
        ),
    ]:
        with pytest.raises(LedgerError) as inactive:
            operation()
        assert inactive.value.code == "parent_archived"
    assert service.list_categories(owner, ledger_id) == []
    assert len(service.list_categories(owner, ledger_id, include_archived=True)) == 2
    service.update_category(
        owner, ledger_id, parent.id, CategoryUpdate(expected_version=2, archived=False)
    )
    service.update_category(
        owner, ledger_id, child.id, CategoryUpdate(expected_version=2, archived=False)
    )
    assert len(service.list_categories(owner, ledger_id)) == 2


def test_archiving_entity_freezes_ledger_but_keeps_history_readable(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity = personal(service, owner, template_key="personal_default")
    account = service.create_account(
        owner,
        entity.ledger.id,
        AccountCreate(name="Fictional cash", kind="cash", asset_ids=["USD"]),
    )
    service.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
    assert service.list_entities(owner) == []
    assert service.list_entities(owner, include_archived=True)[0].id == entity.id
    assert service.list_accounts(owner, entity.ledger.id)[0].id == account.id
    for operation in [
        lambda: service.create_account(
            owner, entity.ledger.id, AccountCreate(name="Bad", kind="bank", asset_ids=["USD"])
        ),
        lambda: service.update_account(
            owner, entity.ledger.id, account.id, AccountUpdate(expected_version=1, name="Bad")
        ),
        lambda: service.create_category(
            owner, entity.ledger.id, CategoryCreate(name="Bad", kind="expense")
        ),
    ]:
        with pytest.raises(LedgerError) as error:
            operation()
        assert error.value.code == "entity_archived"
    service.update_entity(owner, entity.id, EntityUpdate(expected_version=2, archived=False))
    assert len(service.list_entities(owner)) == 1


def test_concurrent_updates_have_one_winner_without_lost_changes(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity = personal(service, owner)
    account = service.create_account(
        owner,
        entity.ledger.id,
        AccountCreate(name="Fictional cash", kind="cash", asset_ids=["USD"]),
    )

    def change(name):
        try:
            return (
                LedgerService(engine)
                .update_account(
                    owner,
                    entity.ledger.id,
                    account.id,
                    AccountUpdate(expected_version=1, name=name),
                )
                .name
            )
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ["First", "Second"]))
    assert results.count("version_conflict") == 1
    stored = service.list_accounts(owner, entity.ledger.id)[0]
    assert stored.version == 2 and stored.name in {"First", "Second"}


def test_concurrent_category_archive_and_create_preserve_active_parent_rule(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entity = personal(service, owner)
    category = service.create_category(
        owner, entity.ledger.id, CategoryCreate(name="Parent", kind="expense")
    )

    def archive():
        try:
            service.update_category(
                owner,
                entity.ledger.id,
                category.id,
                CategoryUpdate(expected_version=1, archived=True),
            )
            return "archived"
        except LedgerError as error:
            return error.code

    def add_child():
        try:
            service.create_category(
                owner,
                entity.ledger.id,
                CategoryCreate(name="Child", kind="expense", parent_id=category.id),
            )
            return "created"
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(archive), pool.submit(add_child)]
        results = {future.result() for future in futures}
    assert results in [{"archived", "parent_archived"}, {"created", "active_children"}]
    with Session(engine) as session:
        parent = session.get(Category, category.id)
        active_children = session.scalar(
            select(func.count())
            .select_from(Category)
            .where(Category.parent_id == category.id, Category.archived.is_(False))
        )
        assert not parent.archived or active_children == 0


def test_deterministic_pagination_and_archive_visibility(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    entities = [personal(service, owner) for _ in range(3)]
    ordered = sorted(entities, key=lambda item: item.id)
    assert service.list_entities(owner, limit=1, offset=1)[0].id == ordered[1].id
    assert service.list_entities(owner, offset=3) == []
    assets = service.list_assets(owner)
    assert service.list_assets(owner, limit=2, offset=2) == assets[2:4]
