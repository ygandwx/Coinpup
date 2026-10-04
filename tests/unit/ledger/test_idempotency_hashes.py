"""Frozen published hashes and raw request identity across future schema defaults."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from coinpup_api.ledger.idempotency import (
    command_hash,
    command_hash_v2,
    receipt_hash_version,
    revision_hash,
    revision_hash_v2,
)
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
    ExchangeReplacement,
    ExpenseCreate,
    ExpenseReplacement,
    FeeCreate,
    IncomeCreate,
    IncomeReplacement,
    OpeningCreate,
    OpeningReplacement,
    PostingSplit,
    TransferCreate,
    TransferReplacement,
)
from coinpup_api.ledger.service import LedgerError
from pydantic import Field, create_model

LEDGER, ACCOUNT, DESTINATION, CATEGORY, CATEGORY2, OPERATION, CLIENT_ID = (
    UUID(f"00000000-0000-4000-8000-{number:012d}") for number in range(1, 8)
)
CREATES = {
    "opening": OpeningCreate,
    "income": IncomeCreate,
    "expense": ExpenseCreate,
    "transfer": TransferCreate,
    "exchange": ExchangeCreate,
}
REPLACEMENTS = {
    "opening": OpeningReplacement,
    "income": IncomeReplacement,
    "expense": ExpenseReplacement,
    "transfer": TransferReplacement,
    "exchange": ExchangeReplacement,
}
# Captured from the published implementation before OPT-20. Never regenerate on failures.
V1_GOLDENS = {
    "opening": "276460a858c7737d0894503880463283c35369653372519d1ebf9973c0794459",
    "correct-opening": "68d48c9288f48a5c34c81be6a3b977e8c2e8cb34be12325e693f419fdc5ecdd9",
    "income": "d298e305638f00e934c522a2d6259580c8b2d4f02331644b62c9b17165b5d4e2",
    "correct-income": "191342fc15b415094fce1660db95f576e9559286bc9d1ea7786bab228ea199a1",
    "income-fees": "16b5bb5655e0d80b874673e9577664ead87210518d0a3cdeae2b2d49f121c8a8",
    "correct-income-fees": "e67bbb8b207db447335ce73f109642a55d88ad4a7e52b2f5c38eab05dd04e518",
    "expense": "88fcb3c5894365ca3739032076ebc4950275f05194bdfbcbc8bf1c3c19300278",
    "correct-expense": "711db364e5c514e050d32830fbddb68cb2888b8f9c905c0d00a067b7fe98efb9",
    "expense-fees": "9d7ad16d8624b25c258b2f582a7942ac4bebe371f75f08f688ff27f27c7c4155",
    "correct-expense-fees": "8bbbc4267f844b86f431d36250ac00d43804032991096ea76dc2ee895275a18e",
    "transfer": "dd276332ec2b62ea8bb757d9868c1b4b1f41bdfd98faf85dd9ef86494f4a476d",
    "correct-transfer": "635f32448d2254618cf9007117f57a5a11088d3aaf017f1985995309ce5ec808",
    "transfer-fees": "f1a01bfb2f9ebe055afd99c4b447e6deb1cd6e01c92ecf3c5fdb8f5590f4a675",
    "correct-transfer-fees": "809d3432ac2457cf8e8728e2e164ecb7ef41a12142815b353900a1f33e32488c",
    "exchange": "3d5f0420cfce131423299b4eb42964ff02bb58c47de2f78679a5756cfe2d36b7",
    "correct-exchange": "f8553f82c95e763409c9cddd4520da2f94b72c8b37a5072c3bbb9b7cdcd4527b",
    "exchange-fees": "cfb3952bc83c81c025c9668971217a5b0c12d9006d72ef65474c9862a60f7db2",
    "correct-exchange-fees": "a60115d2454285c3122b23961f6dc6ce67502680112238b2103e5412bce866c5",
    "cancel": "6b24ec98299590a0e31eea56ecf05249028a2afa613103a645ce5a3b3b793780",
}
# Independently calculated from the documented v2 envelope and the literal route templates.
V2_GOLDENS = {
    "opening": "18b7e9ab3a921769a47675a8fe5b8550154dcc2c7c95afe3c88678b78723f488",
    "income": "12ba9c695c826ce5e956758bcedb00c7cc2f017dbbdd801e46f8f4e39f09fd46",
    "expense": "389438bdfde0c95123f6cbe265399cd3b2a4920cf0c31bb2a60c03e5eb614611",
    "transfer": "6243a299a2215a81ff36e93f1ef596d36042129c8250fadc61b5ad6eca30233f",
    "exchange": "c5f8f9ba0a4dffc792b60d8a487e8009b200e3c3378f6c3c568eac14e2ef072d",
    "correct-expense": "c2241459b428dc29b7157b448b10dec434ef2d5f1652400c90a3e49f78fdf007",
    "cancel": "aa43ebc8dceb17206c36bb93b9c649205fb09a1b7288ff8695561f4e6ee799e2",
}
FutureFee = create_model("FutureFee", __base__=FeeCreate, future_fee=(str, "Fictional fee"))
FutureSplit = create_model(
    "FutureSplit", __base__=PostingSplit, future_split=(str, "Fictional split")
)


def future_model(base):
    # Retype the containers: BaseModel serialization otherwise discards subclass-only fields.
    fields = {"future_default": (str, "Fictional default")}
    if "fees" in base.model_fields:
        fields["fees"] = (list[FutureFee], Field(default_factory=list))
    if "splits" in base.model_fields:
        fields["splits"] = (list[FutureSplit], ...)
    return create_model("Future" + base.__name__, __base__=base, **fields)


FUTURE_CREATES = {kind: future_model(cls) for kind, cls in CREATES.items()}
FUTURE_REPLACEMENTS = {kind: future_model(cls) for kind, cls in REPLACEMENTS.items()}


def request_body(case):
    if case == "cancel":
        return {"expected_version": 2, "reason": "  取消虚构记录 / Cancel  "}
    kind = case.removeprefix("correct-").removesuffix("-fees")
    body = {
        "id": str(CLIENT_ID),
        "transaction_date": "2026-01-02",
        "description": "虚构账务 / Fictional",
    }
    if kind == "exchange":
        body.update(
            source_account_id=str(ACCOUNT),
            source_asset_id="USD",
            source_amount="100.00",
            destination_account_id=str(DESTINATION),
            destination_asset_id="EUR",
            destination_amount="90.00",
        )
    else:
        body.update(asset_id="USD", amount="10.00")
        if kind == "transfer":
            body.update(source_account_id=str(ACCOUNT), destination_account_id=str(DESTINATION))
        else:
            body["account_id"] = str(ACCOUNT)
    if kind in {"income", "expense"}:
        body.update(
            recognition_date="2026-01-01",
            splits=[
                {"category_id": str(CATEGORY), "amount": "8.00"},
                {"category_id": str(CATEGORY2), "amount": "2.00"},
            ],
        )
    if case.endswith("-fees"):
        body["fees"] = [
            {
                "account_id": str(ACCOUNT),
                "asset_id": "USD",
                "amount": "2.00",
                "category_id": str(CATEGORY),
            },
            {
                "account_id": str(DESTINATION),
                "asset_id": "BTC",
                "amount": "0.00001000",
                "category_id": str(CATEGORY2),
            },
        ]
    if case.startswith("correct-"):
        body.pop("id")
        return {
            "expected_version": 2,
            "reason": "  更正虚构记录 / Correct  ",
            "replacement": body | {"kind": kind},
        }
    return body


def payload_for(case, *, future=False, explicit_future=False):
    body = request_body(case)
    if case == "cancel":
        cls = future_model(CancellationCreate) if future else CancellationCreate
    elif case.startswith("correct-"):
        kind = body["replacement"]["kind"]
        cls = (
            create_model(
                "FutureCorrection",
                __base__=CorrectionCreate,
                replacement=(FUTURE_REPLACEMENTS[kind], ...),
                future_correction=(str, "Fictional"),
            )
            if future
            else CorrectionCreate
        )
    else:
        kind = case.removesuffix("-fees")
        cls = (FUTURE_CREATES if future else CREATES)[kind]
    if explicit_future:
        posting = body.get("replacement", body)
        posting["future_default"] = "Fictional default"
        for name, field in (("fees", "future_fee"), ("splits", "future_split")):
            for item in posting.get(name, []):
                item[field] = "Fictional explicitly provided value"
        if "replacement" in body:
            body["future_correction"] = "Fictional"
    return cls.model_validate(body)


def digest(case, payload, *, version=1, raw_body=None, ledger=LEDGER, operation=OPERATION):
    if case == "cancel" or case.startswith("correct-"):
        action = "cancel" if case == "cancel" else "correct"
        if version == 1:
            return revision_hash(action, ledger, operation, payload)
        return revision_hash_v2(action, ledger, operation, payload, raw_body=raw_body)
    kind = case.removesuffix("-fees")
    if version == 1:
        return command_hash(kind, ledger, payload)
    return command_hash_v2(kind, ledger, payload, raw_body=raw_body)


def encoded(body):
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("case", V1_GOLDENS)
def test_all_published_v1_command_and_revision_shapes_remain_frozen(case):
    assert digest(case, payload_for(case)) == V1_GOLDENS[case]


@pytest.mark.parametrize("explicit_future", [False, True])
@pytest.mark.parametrize("case", V1_GOLDENS)
def test_v1_freezes_future_fields_at_every_typed_nesting_level(case, explicit_future):
    original = payload_for(case)
    future = payload_for(case, future=True, explicit_future=explicit_future)
    assert future.model_dump(mode="json") != original.model_dump(mode="json")
    posting = future.model_dump(mode="json").get("replacement", future.model_dump(mode="json"))
    for name, field in (("fees", "future_fee"), ("splits", "future_split")):
        for item in posting.get(name, []):
            assert field in item
    assert digest(case, future) == V1_GOLDENS[case]


@pytest.mark.parametrize("case", V1_GOLDENS)
def test_v2_uses_unchanged_received_json_despite_future_typed_defaults(case):
    raw = encoded(request_body(case))
    original, future = payload_for(case), payload_for(case, future=True)
    assert original.model_dump(mode="json") != future.model_dump(mode="json")
    assert digest(case, original, version=2, raw_body=raw) == digest(
        case, future, version=2, raw_body=raw
    )


@pytest.mark.parametrize("case", V2_GOLDENS)
def test_v2_hash_contains_the_published_route_action_and_identity_envelope(case):
    assert (
        digest(case, payload_for(case), version=2, raw_body=encoded(request_body(case)))
        == V2_GOLDENS[case]
    )


def test_v1_typed_replacement_with_default_kind_retains_discriminator():
    body = request_body("correct-expense")
    default_kind = create_model(
        "DefaultKindReplacement",
        __base__=ExpenseReplacement,
        kind=(ExpenseReplacement.model_fields["kind"].annotation, "expense"),
    )
    replacement = default_kind.model_validate(
        {field: value for field, value in body["replacement"].items() if field != "kind"}
    )
    assert "kind" not in replacement.model_fields_set
    payload = CorrectionCreate(expected_version=2, reason=body["reason"], replacement=replacement)
    assert digest("correct-expense", payload) == V1_GOLDENS["correct-expense"]


def test_v2_ignores_nested_object_key_order_and_json_whitespace():
    body = request_body("expense-fees")

    def reordered(value):
        if isinstance(value, dict):
            return {key: reordered(value[key]) for key in reversed(value)}
        if isinstance(value, list):
            return [reordered(item) for item in value]
        return value

    payload = payload_for("expense-fees")
    pretty = json.dumps(reordered(body), ensure_ascii=False, indent=3).encode()
    assert digest("expense-fees", payload, version=2, raw_body=pretty) == digest(
        "expense-fees", payload, version=2, raw_body=encoded(body)
    )


@pytest.mark.parametrize("field", ["fees", "splits", "amount"])
def test_v2_preserves_array_order_and_exact_amount_spelling(field):
    original = request_body("expense-fees")
    changed = deepcopy(original)
    changed[field] = "10.0" if field == "amount" else list(reversed(changed[field]))
    payload = payload_for("expense-fees")
    assert digest("expense-fees", payload, version=2, raw_body=encoded(original)) != digest(
        "expense-fees", payload, version=2, raw_body=encoded(changed)
    )


@pytest.mark.parametrize("field,value", [("id", None), ("fees", []), ("description", "")])
def test_v2_distinguishes_omission_from_explicit_null_or_default(field, value):
    body = request_body("income")
    body.pop(field, None)
    payload = IncomeCreate.model_validate(body)
    explicit = body | {field: value}
    assert payload.model_dump() == IncomeCreate.model_validate(explicit).model_dump()
    assert command_hash_v2("income", LEDGER, payload, raw_body=encoded(body)) != command_hash_v2(
        "income", LEDGER, payload, raw_body=encoded(explicit)
    )
    assert command_hash_v2("income", LEDGER, payload) == command_hash_v2(
        "income", LEDGER, payload, raw_body=encoded(body)
    )


def test_v2_preserves_received_revision_reason_before_validation_trims_it():
    body = request_body("cancel")
    payload = payload_for("cancel")
    trimmed = body | {"reason": payload.reason}
    assert CancellationCreate.model_validate(trimmed) == payload
    assert digest("cancel", payload, version=2, raw_body=encoded(body)) != digest(
        "cancel", payload, version=2, raw_body=encoded(trimmed)
    )


@pytest.mark.parametrize("case", V1_GOLDENS)
def test_v2_is_scoped_to_ledger_and_revision_operation(case):
    payload, raw = payload_for(case), encoded(request_body(case))
    original = digest(case, payload, version=2, raw_body=raw)
    assert original != digest(case, payload, version=2, raw_body=raw, ledger=DESTINATION)
    if case == "cancel" or case.startswith("correct-"):
        assert original != digest(case, payload, version=2, raw_body=raw, operation=DESTINATION)


def test_v2_distinguishes_command_actions_and_their_route_templates():
    payload, raw = payload_for("expense"), encoded(request_body("expense"))
    assert command_hash_v2("expense", LEDGER, payload, raw_body=raw) != command_hash_v2(
        "income", LEDGER, payload, raw_body=raw
    )
    assert revision_hash_v2(
        "correct", LEDGER, OPERATION, payload, raw_body=raw
    ) != revision_hash_v2("cancel", LEDGER, OPERATION, payload, raw_body=raw)


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b'{"extra":NaN}', b'{"extra":Infinity}'])
def test_v2_rejects_invalid_or_non_finite_json_with_stable_error(raw):
    with pytest.raises(LedgerError) as error:
        command_hash_v2("opening", LEDGER, payload_for("opening"), raw_body=raw)
    assert (error.value.code, error.value.status) == ("invalid_request", 422)
    assert raw.decode() not in str(error.value)


@pytest.mark.parametrize("version", [0, 3, None])
def test_unknown_stored_hash_version_never_falls_back_to_new_request(version):
    with pytest.raises(LedgerError) as error:
        receipt_hash_version(SimpleNamespace(hash_version=version))
    assert (error.value.code, error.value.status) == ("ledger_integrity", 503)
    assert receipt_hash_version(None) == 2
