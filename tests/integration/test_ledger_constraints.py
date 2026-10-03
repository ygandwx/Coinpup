"""PostgreSQL remains a boundary even when SQL bypasses the service layer."""

import os
import uuid

import pytest
from coinpup_api.ledger.assets import ASSETS, AssetDefinition
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, Category, Entity, Ledger
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]


@pytest.fixture
def structure_rows(structure_database):
    engine, owner_id = structure_database
    entity_ids = [uuid.uuid4(), uuid.uuid4()]
    ledger_ids = [uuid.uuid4(), uuid.uuid4()]
    account_id = uuid.uuid4()
    category_id = uuid.uuid4()
    with engine.begin() as connection:
        for entity_id, ledger_id in zip(entity_ids, ledger_ids, strict=True):
            connection.execute(
                insert(Entity).values(
                    id=entity_id, owner_id=owner_id, kind="personal", name="Fictional person"
                )
            )
            connection.execute(
                insert(Ledger).values(
                    id=ledger_id, entity_id=entity_id, owner_id=owner_id, base_asset_id="USD"
                )
            )
        connection.execute(
            insert(Account).values(
                id=account_id, ledger_id=ledger_ids[0], name="Fictional wallet", kind="cash"
            )
        )
        connection.execute(
            insert(Category).values(
                id=category_id, ledger_id=ledger_ids[0], name="Fictional expense", kind="expense"
            )
        )
    return engine, owner_id, entity_ids, ledger_ids, account_id, category_id


def test_migration_seeds_exact_builtin_asset_definitions(structure_database):
    engine, _ = structure_database
    with engine.connect() as connection:
        records = connection.execute(select(AssetRecord.__table__)).mappings().all()
    definitions = {
        row["asset_id"]: AssetDefinition(
            row["code"], row["kind"], row["scale"], row["network"], row["token_reference"]
        )
        for row in records
    }
    assert definitions == ASSETS


@pytest.mark.parametrize(
    "values",
    [
        {"asset_id": "jpy", "code": "jpy", "kind": "fiat", "scale": 0},
        {"asset_id": "JPY", "code": "JPY", "kind": "fiat", "scale": 19},
        {"asset_id": "OTHER", "code": "JPY", "kind": "fiat", "scale": 0},
        {"asset_id": "TEST", "code": "TEST", "kind": "native", "scale": 2},
        {"asset_id": "T", "code": "USDT", "kind": "token", "scale": 6},
        {
            "asset_id": "token:eth:fixture",
            "code": "USDT",
            "kind": "token",
            "scale": 6,
            "network": "ETH",
            "token_reference": "fixture",
        },
    ],
)
def test_asset_identity_and_precision_reject_invalid_insert(structure_database, values):
    engine, _ = structure_database
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(insert(AssetRecord).values(**values))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_id", "KRW"),
        ("code", "KRW"),
        ("kind", "token"),
        ("scale", 1),
        ("network", "fictional"),
        ("token_reference", "fictional"),
    ],
)
def test_asset_identity_is_frozen_even_before_reference(structure_database, field, value):
    engine, _ = structure_database
    with engine.begin() as connection:
        connection.execute(
            insert(AssetRecord).values(asset_id="JPY", code="JPY", kind="fiat", scale=0)
        )
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                update(AssetRecord).where(AssetRecord.asset_id == "JPY").values(**{field: value})
            )
    assert rejected.value.orig.diag.constraint_name == "ck_structure_immutable"


def test_asset_availability_changes_without_changing_unit(structure_database):
    engine, _ = structure_database
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                update(AssetRecord)
                .where(AssetRecord.asset_id == "USD")
                .values(enabled=False, version=2)
            )
            row = (
                connection.execute(
                    select(AssetRecord.__table__).where(AssetRecord.asset_id == "USD")
                )
                .mappings()
                .one()
            )
            assert row["enabled"] is False
            assert row["version"] == 2
            assert row["scale"] == 2
        finally:
            transaction.rollback()


def test_account_asset_cannot_claim_another_ledger(structure_rows):
    engine, _, _, ledgers, account_id, _ = structure_rows
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                insert(AccountAsset).values(
                    account_id=account_id, ledger_id=ledgers[1], asset_id="USD"
                )
            )
    assert rejected.value.orig.diag.constraint_name == "fk_account_assets_account_ledger"


@pytest.mark.parametrize("wrong_field", ["ledger", "kind"])
def test_category_parent_must_match_ledger_and_kind(structure_rows, wrong_field):
    engine, _, _, ledgers, _, parent_id = structure_rows
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                insert(Category).values(
                    id=uuid.uuid4(),
                    ledger_id=ledgers[1] if wrong_field == "ledger" else ledgers[0],
                    name="Fictional child",
                    kind="income" if wrong_field == "kind" else "expense",
                    parent_id=parent_id,
                )
            )
    assert rejected.value.orig.diag.constraint_name == "fk_categories_parent_ledger_kind"


def test_multirow_insert_cannot_create_a_category_cycle(structure_rows):
    engine, _, _, ledgers, _, _ = structure_rows
    first, second = uuid.uuid4(), uuid.uuid4()
    rows = [
        {
            "id": identifier,
            "parent_id": parent,
            "ledger_id": ledgers[0],
            "kind": "expense",
            "name": "Fictional cyclic category",
        }
        for identifier, parent in ((first, second), (second, first))
    ]
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            # One SQL INSERT statement: the FK alone would allow the two-node cycle.
            connection.execute(insert(Category).values(rows))
    assert rejected.value.orig.diag.constraint_name == "fk_categories_parent_ledger_kind"
    with engine.connect() as connection:
        assert (
            connection.execute(select(Category.id).where(Category.id.in_([first, second]))).all()
            == []
        )


def test_category_parent_can_precede_child_but_cannot_be_reassigned(structure_rows):
    engine, _, _, ledgers, _, parent_id = structure_rows
    child_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Category).values(
                id=child_id,
                parent_id=parent_id,
                ledger_id=ledgers[0],
                kind="expense",
                name="Fictional child",
            )
        )
        connection.execute(
            update(Category)
            .where(Category.id == child_id)
            .values(name="Renamed fictional child", archived=True, version=2)
        )
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                update(Category).where(Category.id == child_id).values(parent_id=None)
            )
    assert rejected.value.orig.diag.constraint_name == "ck_structure_immutable"


@pytest.mark.parametrize(
    "target", ["entity_kind", "entity_owner", "ledger_entity", "ledger_base", "account_ledger"]
)
def test_structure_ownership_is_immutable(structure_rows, target):
    engine, _, entities, ledgers, account_id, _ = structure_rows
    statements = {
        "entity_kind": update(Entity).where(Entity.id == entities[0]).values(kind="company"),
        "entity_owner": update(Entity)
        .where(Entity.id == entities[0])
        .values(owner_id=uuid.uuid4()),
        "ledger_entity": update(Ledger)
        .where(Ledger.id == ledgers[0])
        .values(entity_id=entities[1]),
        "ledger_base": update(Ledger).where(Ledger.id == ledgers[0]).values(base_asset_id="EUR"),
        "account_ledger": update(Account)
        .where(Account.id == account_id)
        .values(ledger_id=ledgers[1]),
    }
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(statements[target])
    assert rejected.value.orig.diag.constraint_name == "ck_structure_immutable"


def test_ledger_owner_must_match_entity_owner(structure_database):
    engine, owner_id = structure_database
    entity_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Entity).values(
                id=entity_id, owner_id=owner_id, name="Fictional person", kind="personal"
            )
        )
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                insert(Ledger).values(
                    id=uuid.uuid4(),
                    entity_id=entity_id,
                    owner_id=uuid.uuid4(),
                    base_asset_id="USD",
                )
            )
    assert rejected.value.orig.diag.constraint_name == "fk_ledgers_entity_owner"
