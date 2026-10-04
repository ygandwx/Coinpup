"""Real pre-version receipts survive upgrades; new receipts hash the original JSON."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.posting_schemas import (
    CorrectionCreate,
    ExchangeCreate,
    ExpenseCreate,
    ExpenseReplacement,
    FeeCreate,
    IncomeCreate,
    PostingSplit,
)
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerService
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


class FutureFee(FeeCreate):
    future_fee_source: str | None = None


class FutureSplit(PostingSplit):
    future_project: str | None = None


class FutureIncome(IncomeCreate):
    future_source: str | None = None
    fees: list[FutureFee] = Field(default_factory=list)
    splits: list[FutureSplit]


class FutureExchange(ExchangeCreate):
    future_source: str | None = None
    fees: list[FutureFee] = Field(default_factory=list)


class FutureExpenseReplacement(ExpenseReplacement):
    future_source: str | None = None
    fees: list[FutureFee] = Field(default_factory=list)
    splits: list[FutureSplit]


class FutureCorrection(CorrectionCreate):
    future_source: str | None = None
    replacement: FutureExpenseReplacement


def raw_json(body):
    return json.dumps(body, ensure_ascii=False, allow_nan=False).encode()


def receipt_versions(engine):
    with Session(engine) as session:
        return dict(session.execute(select(CommandReceipt.key, CommandReceipt.hash_version)).all())


def test_real_0008_receipts_replay_seven_routes_after_upgrade_and_archive(
    legacy_v1_receipts, authenticated_client
):
    s = legacy_v1_receipts
    client, engine, owner = authenticated_client
    assert s["owner"] == owner
    before = s["before_upgrade"]
    assert set(receipt_versions(engine).values()) == {1}

    def replay_all():
        for case in s["cases"].values():
            response = client.post(
                f"/api/v1/ledgers/{s['ledger']}" + case["suffix"],
                content=raw_json(case["body"]),
                headers={"Content-Type": "application/json", "Idempotency-Key": case["key"]},
            )
            assert response.status_code == case["status"], response.text
            assert response.json() == case["response"]
        assert s["snapshot"](engine) == before
        assert set(receipt_versions(engine).values()) == {1}

    replay_all()
    correction = s["cases"]["correct"]["response"]
    cancellation = s["cases"]["cancel"]["response"]
    current = client.get(f"/api/v1/ledgers/{s['ledger']}/operations/{correction['id']}")
    assert current.json() == cancellation
    assert (correction["status"], correction["version"]) == ("active", 2)
    assert (cancellation["status"], cancellation["version"]) == ("cancelled", 3)

    structure = LedgerService(engine)
    structure.update_asset(owner, "USD", AssetUpdate(expected_version=1, enabled=False))
    structure.update_entity(owner, s["entity"].id, EntityUpdate(expected_version=1, archived=True))
    replay_all()


def test_v1_only_history_survives_real_hash_version_migration_round_trip(legacy_v1_receipts):
    s = legacy_v1_receipts
    config = Config(str(ROOT / "alembic.ini"))
    try:
        command.downgrade(config, "20261003_0008")
        with s["engine"].connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261003_0008"
            )
        assert s["snapshot"](s["engine"]) == s["before_upgrade"]
    finally:
        command.upgrade(config, "head")
    assert s["snapshot"](s["engine"]) == s["before_upgrade"]
    assert set(receipt_versions(s["engine"]).values()) == {1}


@pytest.mark.parametrize("kind", ["income", "exchange", "correct"])
@pytest.mark.parametrize("hash_version", [1, 2])
def test_persisted_receipts_ignore_future_optional_model_defaults(
    legacy_v1_receipts, kind, hash_version
):
    s = legacy_v1_receipts
    case = s["cases"][kind]
    body, key = case["body"], case["key"]
    classes = {"income": IncomeCreate, "exchange": ExchangeCreate, "correct": CorrectionCreate}
    future_classes = {
        "income": FutureIncome,
        "exchange": FutureExchange,
        "correct": FutureCorrection,
    }
    method = (
        s["posting"].correct_operation
        if kind == "correct"
        else getattr(s["posting"], "post_" + kind)
    )
    operation = UUID(case["response"]["id"])
    if hash_version == 2:
        key = "future-v2-" + kind
        if kind == "correct":
            original_body = s["cases"]["revision_original"]["body"]
            original = s["posting"].post_expense(
                s["owner"],
                s["ledger"],
                ExpenseCreate.model_validate(original_body),
                "future-v2-original",
                raw_body=raw_json(original_body),
            )
            operation = original.id
        args = (
            (s["owner"], s["ledger"], operation) if kind == "correct" else (s["owner"], s["ledger"])
        )
        expected = method(
            *args, classes[kind].model_validate(body), key, raw_body=raw_json(body)
        ).model_dump(mode="json")
    else:
        args = (
            (s["owner"], s["ledger"], operation) if kind == "correct" else (s["owner"], s["ledger"])
        )
        expected = case["response"]
    payload = future_classes[kind].model_validate(body)
    serialized = payload.model_dump(mode="json")
    assert serialized["future_source"] is None
    nested = serialized["replacement"] if kind == "correct" else serialized
    assert nested["fees"][0]["future_fee_source"] is None
    if kind != "exchange":
        assert nested["splits"][0]["future_project"] is None
    before = s["snapshot"](s["engine"])
    replay = method(*args, payload, key, raw_body=raw_json(body))
    assert replay.model_dump(mode="json") == expected
    assert receipt_versions(s["engine"])[key] == hash_version
    assert s["snapshot"](s["engine"]) == before


@pytest.mark.parametrize("difference", ["fees", "null_id", "description", "amount_spelling"])
def test_new_v2_receipts_reject_changed_presence_and_preserve_original_json(
    legacy_v1_receipts, authenticated_client, difference
):
    s = legacy_v1_receipts
    client, engine, _ = authenticated_client
    body = s["cases"]["expense"]["body"]
    key = "new-v2-body"
    path = f"/api/v1/ledgers/{s['ledger']}/expenses"
    headers = {"Idempotency-Key": key}
    original = client.post(path, json=body, headers=headers)
    assert original.status_code == 201, original.text
    assert receipt_versions(engine)[key] == 2
    before = s["snapshot"](engine)
    # JSON whitespace and object-key order are canonicalized; monetary spelling is not.
    reordered = dict(reversed(list(body.items())))
    retry = client.post(
        path,
        content=json.dumps(reordered, indent=2).encode(),
        headers=headers | {"Content-Type": "application/json"},
    )
    assert retry.status_code == 201 and retry.json() == original.json()
    changed = (
        body | {"fees": []}
        if difference == "fees"
        else body | {"id": None}
        if difference == "null_id"
        else body | {"description": ""}
    )
    if difference == "amount_spelling":
        changed = body | {"amount": "100.0", "splits": [body["splits"][0] | {"amount": "100.0"}]}
    conflict = client.post(path, json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert s["snapshot"](engine) == before


def test_real_downgrade_rejects_v2_receipts_without_changing_history(
    legacy_v1_receipts, authenticated_client
):
    s = legacy_v1_receipts
    client, engine, _ = authenticated_client
    response = client.post(
        f"/api/v1/ledgers/{s['ledger']}/expenses",
        json=s["cases"]["expense"]["body"],
        headers={"Idempotency-Key": "protect-v2-history"},
    )
    assert response.status_code == 201, response.text
    before, versions = s["snapshot"](engine), receipt_versions(engine)
    with engine.connect() as connection:
        head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(IntegrityError) as rejected:
        command.downgrade(Config(str(ROOT / "alembic.ini")), "20261003_0008")
    assert rejected.value.orig.diag.constraint_name == "ck_command_receipts_hash_version_downgrade"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert s["snapshot"](engine) == before
    assert receipt_versions(engine) == versions and versions["protect-v2-history"] == 2
