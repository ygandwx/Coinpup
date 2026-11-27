"""Asset identity and amount precision across the catalog, API, and PostgreSQL."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, AssetCreate, EntityCreate
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration
_PREFIX = "/api/v1/assets"


def test_presets_and_verified_status_through_authenticated_api(authenticated_client):
    client, _, _ = authenticated_client
    response = client.get(f"{_PREFIX}/presets")
    assert response.status_code == 200
    presets = response.json()
    assert len([item for item in presets if item["kind"] == "native"]) == 14
    assert len([item for item in presets if item["kind"] == "token"]) == 20
    assert all(item["verified_on"] == "2026-11-27" and item["sources"] for item in presets)
    row = next(item for item in presets if item["network"] == "tron" and item["code"] == "USDT")
    payload = {key: row[key] for key in ("code", "kind", "scale", "network", "token_reference")}
    assert client.post(_PREFIX, json=payload | {"scale": 5}).status_code == 422
    assert client.post(_PREFIX, json=payload | {"code": "CUSTOM"}).status_code == 422
    created = client.post(_PREFIX, json=payload)
    assert created.status_code == 201
    assert created.json()["verified"] is True
    assert created.json()["asset_id"] == row["asset_id"]
    custom = client.post(_PREFIX, json=payload | {"token_reference": "FictionalCustomToken"})
    assert custom.status_code == 201
    assert custom.json()["verified"] is False
    assert (
        client.patch(
            f"{_PREFIX}/{custom.json()['asset_id']}", json={"expected_version": 1, "scale": 12}
        ).status_code
        == 422
    )
    client.cookies.clear()
    assert client.get(f"{_PREFIX}/presets").status_code == 401


@pytest.mark.parametrize(
    ("code", "scale", "valid_amount", "invalid_amount"),
    [
        ("T1", 0, "1", "1.1"),
        ("T123456789", 18, "0.000000000000000001", "0.0000000000000000001"),
    ],
)
def test_custom_precision_and_used_identity_remain_exact(
    structure_database, code, scale, valid_amount, invalid_amount
):
    engine, owner = structure_database
    service, posting = LedgerService(engine), PostingService(engine)
    token = service.create_asset(
        owner,
        AssetCreate(
            code=code,
            kind="token",
            scale=scale,
            network="fictional-testnet",
            token_reference=f"Fixture{scale}",
        ),
    )
    ledger = service.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional test ledger", base_asset_id="USD")
    ).ledger
    wallet = service.create_account(
        owner,
        ledger.id,
        AccountCreate(name="Fictional wallet", kind="crypto", asset_ids=[token.asset_id]),
    )
    payload = {
        "account_id": wallet.id,
        "asset_id": token.asset_id,
        "transaction_date": "2026-11-27",
        "description": "Fictional precision test",
    }
    with pytest.raises(LedgerError) as rejected:
        posting.post_opening(
            owner, ledger.id, OpeningCreate(**payload, amount=invalid_amount), "overprecision"
        )
    assert rejected.value.code == "amount_precision"
    receipt = posting.post_opening(
        owner, ledger.id, OpeningCreate(**payload, amount=valid_amount), "minimum-unit"
    )
    assert receipt.amount == valid_amount
    assert posting.balances(owner, ledger.id, account_id=wallet.id)[0].amount == valid_amount
    with pytest.raises(IntegrityError) as immutable:
        with engine.begin() as connection:
            connection.execute(
                update(AssetRecord).where(AssetRecord.asset_id == token.asset_id).values(scale=1)
            )
    assert immutable.value.orig.diag.constraint_name == "ck_structure_immutable"


def _evm_asset(reference, *, network="ethereum"):
    return {
        "asset_id": f"token:{network}:{reference}",
        "code": "USDC",
        "kind": "token",
        "scale": 6,
        "network": network,
        "token_reference": reference,
    }


def test_evm_address_comparison_is_per_network_and_preserves_the_stored_id(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    reference = "0x" + "a1" * 20
    variant = reference.upper()
    values = _evm_asset(reference)
    values.pop("asset_id")
    asset = service.create_asset(owner, AssetCreate(**values))
    assert asset.asset_id == f"token:ethereum:{reference}"
    with pytest.raises(IntegrityError) as duplicate:
        with engine.begin() as connection:
            connection.execute(insert(AssetRecord).values(**_evm_asset(variant)))
    assert duplicate.value.orig.diag.constraint_name == "uq_assets_evm_contract"
    with engine.begin() as connection:
        connection.execute(insert(AssetRecord).values(**_evm_asset(variant, network="base")))
        case_sensitive = [_evm_asset(item, network="solana") for item in ("aRef", "ARef")]
        connection.execute(insert(AssetRecord), case_sensitive)


def test_catalog_upgrade_preserves_legacy_assets_and_rejects_downgrade_after_use(
    structure_database,
):
    engine, owner = structure_database
    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    service = LedgerService(engine)
    legacy_fiat = service.create_asset(owner, AssetCreate(code="SOL", kind="fiat", scale=2))
    reference = "0x" + "b2" * 20
    rows = [_evm_asset(item) for item in (reference, reference.upper())]
    with engine.connect() as connection:
        head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    try:
        command.downgrade(cfg, "20261127_0114")
        with engine.begin() as connection:
            connection.execute(insert(AssetRecord), rows)
    finally:
        command.upgrade(cfg, "head")
    with pytest.raises(IntegrityError) as wrong_scale:
        with engine.begin() as connection:
            connection.execute(
                insert(AssetRecord).values(
                    asset_id="native:solana:SOL",
                    code="SOL",
                    kind="native",
                    scale=6,
                    network="solana",
                )
            )
    assert wrong_scale.value.orig.diag.constraint_name == "ck_assets_identity"
    solana = service.create_asset(
        owner, AssetCreate(code="SOL", kind="native", scale=9, network="solana")
    )
    assert solana.asset_id == "native:solana:SOL"
    assets = {asset.asset_id: asset for asset in service.list_assets(owner)}
    assert assets[legacy_fiat.asset_id] == legacy_fiat
    assert {row["asset_id"] for row in rows} <= assets.keys()
    with pytest.raises(IntegrityError) as rejected:
        command.downgrade(cfg, "20261127_0114")
    assert rejected.value.orig.diag.constraint_name == "ck_assets_catalog_downgrade"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert (
            connection.scalar(
                select(AssetRecord.scale).where(AssetRecord.asset_id == solana.asset_id)
            )
            == 9
        )
