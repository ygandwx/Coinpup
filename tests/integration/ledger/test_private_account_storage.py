"""Account privacy across PostgreSQL upgrades and the owner-facing HTTP boundary."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.ledger.test_card_accounts import TEST_PAN, _ledger
from tests.integration.ledger.test_crypto_account_profiles import _binance

pytestmark = pytest.mark.integration


def _snapshot(engine):
    with engine.connect() as db:
        return {
            table: list(db.scalars(text(f"SELECT to_jsonb(t) FROM {table} t")))
            for table in (
                "accounts",
                "account_assets",
                "change_log",
                "financial_operations",
                "command_receipts",
                "journals",
                "journal_lines",
            )
        }


def _assert_absent(secrets, projection):
    assert not any(secret in projection for secret in secrets), "Private account value was exposed"


@pytest.mark.parametrize("revision", ["20261125_0113", "20261127_0117"])
def test_upgrade_preserves_private_details_and_original_history(authenticated_client, revision):
    client, engine, owner = authenticated_client
    ledger = _ledger(LedgerService(engine), owner, "Fictional historical ledger")
    base = f"/api/v1/ledgers/{ledger}/accounts"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    card, wallet, bank = str(uuid4()), str(uuid4()), str(uuid4())
    marker = uuid4().hex
    bank_number = "FICT-ACCT-" + marker
    issued_to = "Fictional-" + marker
    custom_name = "Fictional Wallet " + marker
    old_key = "custom:" + hashlib.sha256(custom_name.lower().encode()).hexdigest()
    address = "0x" + uuid4().hex + marker[:8]
    secrets = [issued_to]
    has_profiles = revision == "20261127_0117"
    command.downgrade(cfg, revision)
    try:
        with engine.begin() as db:
            db.execute(
                text("""
                    INSERT INTO accounts(id, ledger_id, name, kind, details, version)
                    VALUES (:id, :ledger, 'Fictional legacy card', 'credit_card',
                            CAST(:details AS jsonb), 3)
                """),
                {
                    "id": card,
                    "ledger": ledger,
                    "details": json.dumps(
                        {"cardholder": issued_to, "card_last_four": TEST_PAN[-4:]}
                        | ({"linked_account_number": TEST_PAN} if has_profiles else {})
                    ),
                },
            )
            if not has_profiles:
                db.execute(
                    text("""
                        INSERT INTO accounts(id, ledger_id, name, kind, details)
                        VALUES (:id, :ledger, 'Fictional legacy bank', 'bank',
                                CAST(:details AS jsonb))
                    """),
                    {
                        "id": bank,
                        "ledger": ledger,
                        "details": json.dumps({"account_number": bank_number}),
                    },
                )
                secrets.append(bank_number)
            else:
                db.execute(
                    text("UPDATE accounts SET card_number = :pan WHERE id = :id"),
                    {"id": card, "pan": TEST_PAN},
                )
        if has_profiles:
            with pytest.raises(IntegrityError) as protected_card:
                command.downgrade(cfg, "20261125_0113")
            assert protected_card.value.orig.diag.constraint_name == "ck_accounts_card_downgrade"
            with engine.begin() as db:
                secrets.extend([TEST_PAN, custom_name, marker, address, old_key])
                db.execute(
                    text("""
                        INSERT INTO accounts(id, ledger_id, name, kind, custody_mode,
                            crypto_platform_name, crypto_platform_key, crypto_platform_uid,
                            crypto_platform_email, crypto_platform_note, crypto_profile_used)
                        VALUES (:wallet, :ledger, 'Fictional legacy platform', 'crypto',
                            'onchain', :name, :key, :uid, :email, :note, true)
                    """),
                    {
                        "wallet": wallet,
                        "ledger": ledger,
                        "name": custom_name,
                        "key": old_key,
                        "uid": "uid-" + marker,
                        "email": marker + "@example.test",
                        "note": "note-" + marker,
                    },
                )
                db.execute(
                    text("""
                        INSERT INTO account_assets(account_id, ledger_id, asset_id, crypto_network,
                                                  crypto_address, crypto_memo, crypto_label)
                        VALUES (:wallet, :ledger, 'ETH', 'ethereum', :address, :memo, :label)
                    """),
                    {
                        "wallet": wallet,
                        "ledger": ledger,
                        "address": address,
                        "memo": "memo-" + marker,
                        "label": "label-" + marker,
                    },
                )
            with pytest.raises(IntegrityError) as protected_profile:
                command.downgrade(cfg, "20261127_0116")
            assert (
                protected_profile.value.orig.diag.constraint_name
                == "ck_crypto_account_profile_downgrade"
            )
        changes_before = _snapshot(engine)["change_log"]
    finally:
        command.upgrade(cfg, "head")
    snapshot = _snapshot(engine)
    assert snapshot["change_log"] == changes_before
    _assert_absent(secrets, json.dumps(snapshot))
    listing = client.get(base)
    assert listing.status_code == 200
    records = {row["id"]: row for row in listing.json()}
    assert records[card]["details"] == {"cardholder": issued_to, "card_last_four": TEST_PAN[-4:]}
    if not has_profiles:
        assert records[bank]["details"] == {"account_number": bank_number}
        bank_row = next(item for item in snapshot["accounts"] if item["id"] == bank)
        assert bank_row["details"] == {}
        assert bank_row["details_ciphertext"].startswith("v1.")
    assert records[card]["version"] == 3
    _assert_absent([TEST_PAN], listing.text)
    revealed = client.post(f"{base}/{card}/card-number")
    assert revealed.status_code == 200
    assert revealed.json()["card_number"] == (TEST_PAN if has_profiles else None)
    if has_profiles:
        assert records[wallet]["crypto_platform"] == {
            "custom_name": custom_name,
            "institution_id": None,
            "uid": "uid-" + marker,
            "email": marker + "@example.test",
            "note": "note-" + marker,
        }
        assert records[wallet]["crypto_assets"][0]["details"] == {
            "network": "ethereum",
            "address": address,
            "memo": "memo-" + marker,
            "label": "label-" + marker,
        }
    mobile = client.get(base, params={"card_summary_only": True})
    assert mobile.status_code == 200
    _assert_absent(secrets, mobile.text)
    if has_profiles:
        dormant = client.patch(f"{base}/{card}", json={"expected_version": 3, "kind": "bank"})
        assert dormant.status_code == 200
        _assert_absent([TEST_PAN], dormant.text)
        assert client.post(f"{base}/{card}/card-number").status_code == 404
        cleared = client.patch(f"{base}/{card}", json={"expected_version": 4, "card_number": None})
        assert cleared.status_code == 200
        assert cleared.json()["details"] == {"cardholder": issued_to}
        _assert_absent([TEST_PAN], client.get(base).text)


def test_custom_platform_index_and_ciphertexts_are_scoped(authenticated_client, caplog):
    client, engine, owner = authenticated_client
    service = LedgerService(engine)
    ledgers = [_ledger(service, owner, "Fictional platform ledger") for _ in range(2)]
    marker = uuid4().hex
    payload = _binance()
    platform = payload["crypto_platform"] | {"custom_name": "Fictional platform " + marker}
    del platform["institution_id"]
    payload["crypto_platform"] = platform
    payload["details"] = {"wallet_address": "fictional:" + marker}
    profiles = [asset["details"] for asset in payload["crypto_assets"]]
    private = [
        *platform.values(),
        payload["details"]["wallet_address"],
        profiles[0]["address"],
        profiles[0]["memo"],
        *(asset["label"] for asset in profiles),
    ]
    ids = []
    for ledger in ledgers:
        base = f"/api/v1/ledgers/{ledger}/accounts"
        response = client.post(base, json=payload)
        assert response.status_code == 201
        ids.append(response.json()["id"])
        assert response.json()["crypto_platform"] == platform | {"institution_id": None}
        assert client.post(base, json=payload).status_code == 409
        receipt = client.post(
            f"/api/v1/ledgers/{ledger}/opening-balances",
            json={
                "account_id": ids[-1],
                "asset_id": "custodial:BTC",
                "amount": "1",
                "transaction_date": "2026-11-28",
            },
            headers={"Idempotency-Key": "fictional-opening"},
        )
        assert receipt.status_code == 201
        _assert_absent(private, receipt.text)
        history_path = f"/api/v1/ledgers/{ledger}/operations/{receipt.json()['id']}/history"
        for path, params in (
            (base, {"card_summary_only": True}),
            ("/api/v1/changes", {}),
            (history_path, {}),
            ("/api/v1/reports/mvp", {"ledger_ids": ledger, "view": "balances"}),
        ):
            read = client.get(path, params=params)
            assert read.status_code == 200
            _assert_absent(private, read.text)
            if path == "/api/v1/reports/mvp":
                assert read.json()["rows"]

    storage = _snapshot(engine)
    _assert_absent(private, json.dumps(storage))
    records = {row["id"]: row for row in storage["accounts"]}
    assert records[ids[0]]["crypto_platform_uid_ciphertext"].startswith("v1.")
    assert (
        records[ids[0]]["crypto_platform_uid_ciphertext"]
        != records[ids[1]]["crypto_platform_uid_ciphertext"]
    )
    keys = {records[account]["crypto_platform_key"] for account in ids}
    assert len(keys) == 2
    old_key = hashlib.sha256(platform["custom_name"].lower().encode()).hexdigest()
    assert "custom:" + old_key not in keys
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(
            text(
                "UPDATE accounts SET details = jsonb_build_object('wallet_address', 'legacy') "
                "WHERE id = :id"
            ),
            {"id": ids[0]},
        )
    with pytest.raises(IntegrityError) as plaintext, engine.begin() as db:
        db.execute(
            text("UPDATE accounts SET crypto_platform_uid_ciphertext = 'fictional' WHERE id = :id"),
            {"id": ids[0]},
        )
    assert plaintext.value.orig.diag.constraint_name == "ck_accounts_private_envelopes"
    # An authentic envelope from a different field cannot be presented as this account's UID.
    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE accounts SET crypto_platform_uid_ciphertext = "
                "crypto_platform_email_ciphertext WHERE id = :id"
            ),
            {"id": ids[0]},
        )
    failed = client.get(f"/api/v1/ledgers/{ledgers[0]}/accounts")
    assert failed.status_code == 503
    _assert_absent(private, failed.text + caplog.text)
    unaffected = client.get(f"/api/v1/ledgers/{ledgers[1]}/accounts")
    assert unaffected.status_code == 200
    assert unaffected.json()[0]["crypto_platform"]["uid"] == platform["uid"]


def test_0117_numeric_suffix_is_preserved_privately_and_masked_as_text(authenticated_client):
    from coinpup_api.ledger.card_details import valid_card_number
    from coinpup_api.private_fields import decrypt

    client, engine, owner = authenticated_client
    ledger = _ledger(LedgerService(engine), owner, "Fictional numeric suffix ledger")
    account = uuid4()
    # 0117 compares JSON text to the last four digits, so a JSON number with no
    # leading zero was a database-valid historical spelling.
    prefix = "000000" + str(uuid4().int)[-6:] + "723"
    pan = next(prefix + digit for digit in "0123456789" if valid_card_number(prefix + digit))
    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    command.downgrade(cfg, "20261127_0117")
    try:
        with engine.begin() as db:
            db.execute(
                text("""
                    INSERT INTO accounts(id, ledger_id, name, kind, details, card_number)
                    VALUES (:account, :ledger, 'Fictional numeric card', 'debit_card',
                            jsonb_build_object('card_last_four', CAST(:suffix AS integer)), :pan)
                """),
                {"account": account, "ledger": ledger, "suffix": int(pan[-4:]), "pan": pan},
            )
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as db:
        summary, sealed = db.execute(
            text("SELECT details, details_ciphertext FROM accounts WHERE id = :id"), {"id": account}
        ).one()
    assert summary == {"card_last_four": pan[-4:]}
    assert json.loads(decrypt(sealed, "accounts", "details", ledger, account)) == {
        "card_last_four": int(pan[-4:])
    }
    base = f"/api/v1/ledgers/{ledger}/accounts"
    listed = client.get(base)
    assert listed.status_code == 200 and pan not in listed.text
    assert listed.json()[0]["details"] == summary
    reveal = client.post(f"{base}/{account}/card-number")
    assert reveal.status_code == 200 and reveal.json()["card_number"] == pan
