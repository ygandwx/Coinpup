"""Card storage is versioned, ledger scoped and absent from ordinary projections."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.ledger.schemas import AccountCreate, AccountUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.fixtures.fictional_cards import fictional_pan

pytestmark = pytest.mark.integration
TEST_PAN = fictional_pan()
OTHER_TEST_PAN = fictional_pan()


def _ledger(service, owner, name):
    return service.create_entity(
        owner, EntityCreate(kind="personal", name=name, base_asset_id="USD")
    ).ledger.id


@pytest.mark.parametrize("kind", ["debit_card", "credit_card"])
def test_card_number_never_appears_in_summary_or_sync(authenticated_client, kind):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    ledger = _ledger(structure, owner, "Fictional card ledger")
    other = _ledger(structure, owner, "Fictional isolated ledger")
    base = f"/api/v1/ledgers/{ledger}/accounts"
    payload = {
        "kind": kind,
        "name": "Fictional payment card",
        "asset_ids": ["USD"],
        "card_number": TEST_PAN,
        "details": {
            "institution": "hsbc-hk",
            "cardholder": "Fictional customer",
            "expiry": "06/39",
        },
    }
    created = client.post(base, json=payload)
    assert created.status_code == 201
    assert TEST_PAN not in created.text
    row = created.json()
    assert row["has_card_number"]
    assert row["details"]["card_last_four"] == TEST_PAN[-4:]
    with engine.connect() as db:
        stored = db.execute(
            text("SELECT to_jsonb(a) FROM accounts a WHERE a.id = :account"),
            {"account": row["id"]},
        ).scalar_one()
    assert stored["card_number_ciphertext"].startswith("v1.")
    assert stored["details_ciphertext"].startswith("v1.")
    assert stored["details"].get("card_last_four") == TEST_PAN[-4:]
    assert not any(value in str(stored) for value in (TEST_PAN, payload["details"]["cardholder"]))
    route = f"{base}/{row['id']}"
    reveal_route = route + "/card-number"
    listing = client.get(base)
    assert listing.json() == [row]
    assert TEST_PAN not in listing.text
    assert TEST_PAN not in client.get("/api/v1/changes").text
    slim = client.get(base, params={"card_summary_only": True})
    mobile_row = slim.json()[0]
    assert mobile_row["details"] == {"card_last_four": TEST_PAN[-4:], "expiry": "06/39"}
    assert not mobile_row["has_card_number"]
    assert TEST_PAN not in slim.text
    assert client.get(reveal_route).status_code == 405
    assert (
        client.post(reveal_route, headers={"x-csrf-token": "fictional-invalid"}).status_code == 403
    )
    assert (
        client.post(reveal_route, headers={"origin": "https://example.invalid"}).status_code == 403
    )
    assert (
        client.post(f"/api/v1/ledgers/{other}/accounts/{row['id']}/card-number").status_code == 404
    )
    reveal = client.post(reveal_route)
    assert reveal.status_code == 200 and reveal.headers["cache-control"] == "no-store"
    assert reveal.json() == {"card_number": TEST_PAN}
    assert client.get(base).json()[0]["version"] == 1

    invalid_pan = TEST_PAN[:-1] + str((int(TEST_PAN[-1]) + 1) % 10)
    bad = client.patch(route, json={"expected_version": 1, "card_number": invalid_pan})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_card_details"
    assert invalid_pan not in bad.text
    stale = client.patch(route, json={"expected_version": 2, "card_number": OTHER_TEST_PAN})
    assert stale.status_code == 409 and OTHER_TEST_PAN not in stale.text
    assert client.post(reveal_route).json() == {"card_number": TEST_PAN}
    unrelated = client.patch(
        route,
        json={"expected_version": 1, "details": {"institution": "wise-eu", "expiry": "12/39"}},
    )
    assert unrelated.status_code == 200
    assert unrelated.json()["details"]["card_last_four"] == TEST_PAN[-4:]
    assert client.post(reveal_route).json() == {"card_number": TEST_PAN}
    non_card = client.patch(route, json={"expected_version": 2, "kind": "bank"})
    assert non_card.status_code == 200 and not non_card.json()["has_card_number"]
    assert client.post(reveal_route).status_code == 404
    again = client.patch(route, json={"expected_version": 3, "kind": "credit_card"})
    assert again.status_code == 200 and again.json()["has_card_number"]
    assert client.post(reveal_route).json() == {"card_number": TEST_PAN}
    clear = client.patch(route, json={"expected_version": 4, "card_number": None})
    assert clear.status_code == 200 and not clear.json()["has_card_number"]
    assert "card_last_four" not in clear.json()["details"]
    assert client.post(reveal_route).json() == {"card_number": None}
    client.cookies.clear()
    assert client.post(reveal_route).status_code == 401


@pytest.mark.parametrize(
    "field",
    [
        "CVV",
        "cvv-2",
        "C.V.C",
        "ＰＩＮ",
        "Card PIN",
        "PIN_number",
        "安全码",
        "private_key",
        "seed_phrase",
        "助记词",
    ],
)
def test_card_security_metadata_is_always_rejected(authenticated_client, field):
    client, engine, owner = authenticated_client
    ledger = _ledger(LedgerService(engine), owner, "Fictional security validation")
    base = f"/api/v1/ledgers/{ledger}/accounts"
    rejected = client.post(
        base,
        json={
            "kind": "bank",
            "name": "Fictional account",
            "asset_ids": ["USD"],
            "details": {field: "fictional secret"},
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "invalid_card_details"
    assert "fictional secret" not in rejected.text


def test_card_errors_never_echo_dynamic_keys(authenticated_client):
    client, engine, owner = authenticated_client
    ledger = _ledger(LedgerService(engine), owner, "Fictional validation")
    base = f"/api/v1/ledgers/{ledger}/accounts"
    payload = {"kind": "credit_card", "name": "Fictional card", "asset_ids": ["USD"]}
    for candidate in ({"details": {TEST_PAN: None}}, {TEST_PAN: "not a valid field"}):
        invalid = client.post(base, json=payload | candidate)
        assert invalid.status_code == 422
        assert TEST_PAN not in invalid.text


def test_legacy_suffix_round_trip_and_refuse_used_card_downgrade(structure_database):
    engine, owner = structure_database
    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    with engine.connect() as connection:
        head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    structure = LedgerService(engine)
    ledger = _ledger(structure, owner, "Fictional legacy card")
    legacy = structure.create_account(
        owner,
        ledger,
        AccountCreate(
            name="Fictional suffix only",
            kind="credit_card",
            asset_ids=["USD"],
            details={"card_last_four": "7350"},
        ),
    )
    try:
        command.downgrade(cfg, "20261125_0113")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261125_0113"
            )
            assert (
                connection.scalar(
                    text("SELECT details ->> 'card_last_four' FROM accounts WHERE id = :id"),
                    {"id": legacy.id},
                )
                == "7350"
            )
    finally:
        command.upgrade(cfg, "head")
    assert structure.list_accounts(owner, ledger) == [legacy]
    assert structure.read_card_number(owner, ledger, legacy.id).card_number is None
    debit = structure.update_account(
        owner,
        ledger,
        legacy.id,
        AccountUpdate(expected_version=1, kind="debit_card", card_number=TEST_PAN),
    )
    assert debit.has_card_number
    cleared = structure.update_account(
        owner,
        ledger,
        legacy.id,
        AccountUpdate(expected_version=2, kind="bank", card_number=None),
    )
    assert not cleared.has_card_number
    with pytest.raises(IntegrityError) as error:
        command.downgrade(cfg, "20261125_0113")
    assert error.value.orig.diag.constraint_name == "ck_private_fields_downgrade"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert structure.list_accounts(owner, ledger) == [cleared]
