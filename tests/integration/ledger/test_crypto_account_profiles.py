from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.ledger.assets import CUSTODIAL_PRESETS, NATIVE_PRESETS, TOKEN_PRESETS
from coinpup_api.ledger.schemas import AssetCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.sync.service import ChangeService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration
_TRON_ADDRESS = "41" + "aB" * 20
_BINANCE = {
    "institution_id": "binance",
    "uid": "fictional-generation-17",
    "email": "fixture@example.test",
    "note": "Fictional profile",
}
_COLLECTION = "/api/v1/ledgers/{}/accounts"


def _ledger(engine, owner):
    return (
        LedgerService(engine)
        .create_entity(
            owner,
            EntityCreate(
                kind="personal",
                name="Fictional crypto ledger",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )


def _assert(response, status, code=None):
    assert response.status_code == status, response.text
    value = response.json()
    if code:
        assert value["detail"]["code"] == code
    return value


def _binance():
    return {
        "name": "Fictional platform",
        "kind": "crypto",
        "custody_mode": "custodial",
        "asset_ids": ["custodial:BTC", "custodial:USDT"],
        "crypto_platform": _BINANCE,
        "crypto_preset_asset_ids": ["custodial:BTC", "custodial:USDT"],
        "crypto_assets": [
            {
                "asset_id": "custodial:USDT",
                "details": {
                    "network": "tron",
                    "address": _TRON_ADDRESS,
                    "memo": "fictional memo",
                    "label": "Fictional TRON deposits",
                },
            },
            {"asset_id": "custodial:BTC", "details": {"label": "Fictional BTC holding"}},
        ],
    }


def test_platform_save_is_atomic_scoped_versioned_and_preserves_dormant_profiles(
    authenticated_client,
):
    client, engine, owner = authenticated_client
    ledger, other = _ledger(engine, owner), _ledger(engine, owner)
    account_path, other_path = _COLLECTION.format(ledger), _COLLECTION.format(other)
    payload = _binance()
    invalid = payload | {
        "crypto_assets": [
            {"asset_id": "custodial:BTC", "details": {"network": "tron", "address": _TRON_ADDRESS}}
        ]
    }
    changes = ChangeService(engine)
    baseline = changes.list_changes(owner).next_cursor
    _assert(client.post(account_path, json=invalid), 422, "invalid_crypto_asset_details")
    assert not changes.list_changes(owner, after=baseline).changes
    assert {"custodial:BTC", "custodial:USDT"}.isdisjoint(
        item["asset_id"] for item in _assert(client.get("/api/v1/assets"), 200)
    )

    saved = _assert(client.post(account_path, json=payload), 201)
    assert saved["version"] == 1
    assert saved["crypto_platform"] == _BINANCE | {"custom_name": None}
    assert saved["crypto_assets"][1]["details"]["address"] == _TRON_ADDRESS
    for alias in ("Ｂｉｎａｎｃｅ", "Binance Exchange"):
        _assert(
            client.post(account_path, json=payload | {"crypto_platform": {"custom_name": alias}}),
            422,
            "invalid_crypto_platform",
        )
    picks = {item["asset_id"] for item in _assert(client.get("/api/v1/assets"), 200)}
    assert {asset for asset in picks if asset.startswith("custodial:")} == {
        "custodial:BTC",
        "custodial:USDT",
    }
    _assert(
        client.post(account_path, json=payload | {"name": "Fictional second"}),
        409,
        "duplicate_crypto_platform",
    )
    other_account = _assert(client.post(other_path, json=payload), 201)
    _assert(
        client.patch(
            other_path + "/" + saved["id"], json={"expected_version": 1, "name": "Forbidden"}
        ),
        404,
    )
    mobile = _assert(client.get(account_path, params={"card_summary_only": True}), 200)[0]
    assert mobile["crypto_assets"] == [] and mobile["crypto_platform"] is None
    with pytest.raises(IntegrityError) as immutable, engine.begin() as db:
        db.execute(
            text("UPDATE account_assets SET ledger_id = :ledger WHERE account_id = :account"),
            {"ledger": other, "account": saved["id"]},
        )
    assert immutable.value.orig.diag.constraint_name == "ck_structure_immutable"
    _assert(
        client.post("/api/v1/assets", json={"kind": "custodial", "code": "SOL", "scale": 9}), 201
    )
    with pytest.raises(IntegrityError) as wrong_ledger, engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO account_assets(account_id, ledger_id, asset_id) "
                "VALUES (:account, :ledger, 'custodial:SOL')"
            ),
            {"ledger": other, "account": saved["id"]},
        )
    assert wrong_ledger.value.orig.diag.constraint_name == "fk_account_assets_account_ledger"

    resource = account_path + "/" + saved["id"]
    removed = _assert(
        client.patch(resource, json={"expected_version": 1, "asset_ids": ["custodial:BTC"]}), 200
    )
    assert [item["enabled"] for item in removed["crypto_assets"]] == [True, False]
    assert removed["crypto_assets"][1]["details"]["address"] == _TRON_ADDRESS
    _assert(
        client.patch(resource, json={"expected_version": 1, "crypto_assets": []}),
        409,
        "version_conflict",
    )
    relinked = _assert(
        client.patch(resource, json={"expected_version": 2, "asset_ids": payload["asset_ids"]}), 200
    )
    assert relinked["crypto_assets"] == saved["crypto_assets"]

    cursor = changes.list_changes(owner).next_cursor
    changed = _assert(
        client.patch(
            resource,
            json={
                "expected_version": 3,
                "crypto_assets": [
                    {
                        "asset_id": "custodial:USDT",
                        "details": {
                            "network": "tron",
                            "address": _TRON_ADDRESS,
                            "label": "Respecified",
                        },
                    }
                ],
            },
        ),
        200,
    )
    assert changed["version"] == 4
    notices = changes.list_changes(owner, after=cursor).changes
    assert [(item.entity_type, item.entity_id, item.entity_version) for item in notices] == [
        ("accounts", saved["id"], 4),
        ("account_assets", saved["id"] + ":custodial:USDT", 4),
    ]
    assert changed["crypto_assets"][1]["details"]["memo"] is None
    _assert(client.patch(resource, json={"expected_version": 4, "archived": True}), 200)
    _assert(client.post(account_path, json=payload), 409, "duplicate_crypto_platform")
    cleared = _assert(
        client.patch(
            resource,
            json={
                "expected_version": 5,
                "crypto_platform": None,
                "crypto_assets": [
                    {"asset_id": asset_id, "details": None} for asset_id in payload["asset_ids"]
                ],
            },
        ),
        200,
    )
    assert cleared["crypto_platform"] is None and cleared["crypto_assets"] == []
    _assert(
        client.patch(
            other_path + "/" + other_account["id"],
            json={
                "expected_version": 1,
                "crypto_platform": None,
                "crypto_assets": [
                    {"asset_id": asset_id, "details": None} for asset_id in payload["asset_ids"]
                ],
            },
        ),
        200,
    )

    for raw_change in (
        "UPDATE accounts SET crypto_profile_used = false WHERE id = :account",
        "DELETE FROM accounts WHERE id = :account",
    ):
        with pytest.raises(IntegrityError) as erased, engine.begin() as db:
            db.execute(text(raw_change), {"account": saved["id"]})
        assert erased.value.orig.diag.constraint_name == "ck_accounts_crypto_profile_history"

    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    with pytest.raises(IntegrityError) as failure:
        command.downgrade(cfg, "20261127_0117")
    assert failure.value.orig.diag.constraint_name == "ck_private_fields_downgrade"


def test_legacy_edit_typed_onchain_links_and_custom_platform_normalization(authenticated_client):
    client, engine, owner = authenticated_client
    ledger = _ledger(engine, owner)
    path = _COLLECTION.format(ledger)
    legacy = _assert(
        client.post(
            path,
            json={
                "name": "Fictional old account",
                "kind": "crypto",
                "details": {"wallet_address": "legacy spelling kept", "institution": "tronlink"},
                "asset_ids": ["BTC"],
            },
        ),
        201,
    )
    renamed = _assert(
        client.patch(
            path + "/" + legacy["id"], json={"expected_version": 1, "name": "Fictional rename"}
        ),
        200,
    )
    assert renamed["details"] == legacy["details"] and renamed["crypto_platform"] is None

    payload = {
        "name": "Fictional structured",
        "kind": "crypto",
        "custody_mode": "onchain",
        "asset_ids": ["BTC"],
        "crypto_platform": {"custom_name": "Ｆｉｃｔｉｏｎａｌ  Wallet"},
        "crypto_assets": [{"asset_id": "BTC", "details": {"label": "Independent profile"}}],
    }
    wrong_brand = payload | {"crypto_platform": {"institution_id": "binance"}}
    _assert(client.post(path, json=wrong_brand), 422, "invalid_crypto_platform")
    known_alias = payload | {"crypto_platform": {"custom_name": "欧易 Web3 钱包"}}
    _assert(client.post(path, json=known_alias), 422, "invalid_crypto_platform")
    wrong_network = payload | {
        "crypto_assets": [{"asset_id": "BTC", "details": {"network": "tron"}}]
    }
    _assert(client.post(path, json=wrong_network), 422, "invalid_crypto_asset_details")
    missing_link = payload | {
        "crypto_assets": [{"asset_id": "ETH", "details": {"label": "Missing"}}]
    }
    _assert(client.post(path, json=missing_link), 422, "invalid_crypto_asset_details")
    saved = _assert(client.post(path, json=payload), 201)
    assert saved["crypto_assets"][0]["details"]["network"] == "bitcoin"
    _assert(
        client.post(path, json=payload | {"crypto_platform": {"custom_name": "fictional wallet"}}),
        409,
        "duplicate_crypto_platform",
    )

    _assert(client.patch("/api/v1/assets/BTC", json={"expected_version": 1, "enabled": False}), 200)
    _assert(
        client.post(
            path,
            json=payload
            | {"crypto_platform": {"institution_id": "trezor"}, "crypto_preset_asset_ids": ["BTC"]},
        ),
        409,
        "asset_disabled",
    )
    _assert(
        client.post(
            path,
            json=payload
            | {"asset_ids": ["not-a-preset"], "crypto_preset_asset_ids": ["not-a-preset"]},
        ),
        422,
        "invalid_crypto_asset_details",
    )


def test_typed_per_asset_profiles_can_exceed_the_generic_details_limit(authenticated_client):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    ledger = _ledger(engine, owner)
    verified = [item.definition.asset_id for item in (*NATIVE_PRESETS, *TOKEN_PRESETS)]
    unverified = structure.create_asset(
        owner,
        AssetCreate(
            kind="token",
            code="FICTIONAL9",
            scale=8,
            network="fictional",
            token_reference="FixtureAsset001",
        ),
    )
    assert len(verified) == 34 and len(CUSTODIAL_PRESETS) > 0
    ids = [*verified, unverified.asset_id]
    account = _assert(
        client.post(
            _COLLECTION.format(ledger),
            json={
                "id": str(uuid4()),
                "kind": "crypto",
                "custody_mode": "onchain",
                "name": "Fictional many assets",
                "crypto_platform": {"institution_id": "ledger"},
                "crypto_preset_asset_ids": verified,
                "asset_ids": ids,
                "crypto_assets": [
                    {"asset_id": asset_id, "details": {"label": f"Fixture {index}"}}
                    for index, asset_id in enumerate(ids)
                ],
            },
        ),
        201,
    )
    assert len(account["crypto_assets"]) == 35
    assert {item["asset_id"] for item in account["crypto_assets"]} == set(ids)


def test_asset_details_require_permanent_parent_history(authenticated_client):
    client, engine, owner = authenticated_client
    ledger = _ledger(engine, owner)
    collection = _COLLECTION.format(ledger)
    account = _assert(
        client.post(
            collection,
            json={
                "name": "Fictional onchain wallet",
                "kind": "crypto",
                "custody_mode": "onchain",
                "asset_ids": ["BTC"],
            },
        ),
        201,
    )
    identifiers = {"account": UUID(account["id"]), "ledger": ledger}
    for statement in (
        """
        UPDATE account_assets SET crypto_label_ciphertext = 'Fictional raw update'
        WHERE (account_id, ledger_id, asset_id) = (:account, :ledger, 'BTC')
        """,
        """
        INSERT INTO account_assets(account_id, ledger_id, asset_id, crypto_label_ciphertext)
        VALUES (:account, :ledger, 'ETH', 'Fictional raw insert')
        """,
    ):
        with pytest.raises(IntegrityError) as rejected, engine.begin() as db:
            db.execute(text(statement), identifiers)
        assert (
            rejected.value.orig.diag.constraint_name == "ck_account_assets_crypto_profile_history"
        )

    path = f"{collection}/{account['id']}"
    saved = _assert(
        client.patch(
            path,
            json={
                "expected_version": 1,
                "crypto_assets": [{"asset_id": "BTC", "details": {"label": "Fictional label"}}],
            },
        ),
        200,
    )
    assert saved["crypto_assets"][0]["details"]["label"] == "Fictional label"
    cleared = _assert(
        client.patch(
            path,
            json={"expected_version": 2, "crypto_assets": [{"asset_id": "BTC", "details": None}]},
        ),
        200,
    )
    assert cleared["crypto_assets"] == []
    with engine.connect() as db:
        assert db.scalar(
            text(
                "SELECT crypto_profile_used FROM accounts "
                "WHERE (id, ledger_id) = (:account, :ledger)"
            ),
            identifiers,
        )
