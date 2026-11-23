"""Read fixed metadata without using later, mutable file display facts."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from coinpup_api.business.invoice_completion_facts import verify_invoice_completion
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.errors import MoneyError
from pydantic import ValidationError

from tests.unit.business.test_invoice_reservation_facts import OTHER, OWNER, saved, verify


def evidence(count=1, previous=None):
    reservation, _ = saved(count=count, with_logo=True, uppercase_request=True, previous=previous)
    reservation.created_at = datetime(2026, 11, 13, tzinfo=UTC)
    identity = verify(reservation).identity.model_dump(mode="json")
    files = [
        StoredFile(
            id=UUID(int=index + 80),
            ledger_id=reservation.ledger_id,
            created_by=OWNER,
            sha256=str(index) * 64,
            byte_size=index,
            detected_media_type="application/pdf",
        )
        for index in (1, 2)
    ]
    artifacts = [
        dict(
            language=lang,
            file_id=str(file.id),
            sha256=file.sha256,
            byte_size=file.byte_size,
            media_type="application/pdf",
        )
        for lang, file in zip(("zh", "en"), files, strict=True)
    ]
    completion = InvoiceCompletion(
        id=reservation.id,
        ledger_id=reservation.ledger_id,
        created_by=OWNER,
        created_at=reservation.created_at,
        version=1,
        response_status=201,
        zh_file_id=files[0].id,
        en_file_id=files[1].id,
        response=identity | dict(state="complete", artifacts=artifacts),
    )
    return reservation, completion, *files


def read(res, completed, zh, en, *, owner=OWNER, ledger=None):
    return verify_invoice_completion(
        res, completed, zh_file=zh, en_file=en, owner_id=owner, ledger_id=ledger or res.ledger_id
    )


@pytest.mark.parametrize("count,previous", [(1, None), (200, UUID(int=111))])
def test_verified_receipt_is_detached_ordered_and_unaffected_by_archival(count, previous):
    res, completed, zh, en = evidence(count, previous)
    zh.archived, zh.title, en.version = True, "Fictional renamed original", 27
    completed.created_at = res.created_at.astimezone(timezone(timedelta(hours=-7)))
    completed.response = dict(reversed(list(completed.response.items())))
    received = read(res, completed, zh, en)
    assert received.model_dump(mode="json") == completed.response
    assert received.previous_issuance_id == previous
    completed.response["artifacts"][0]["sha256"] = "f" * 64
    zh.sha256 = "e" * 64
    assert received.artifacts[0].sha256 == "1" * 64
    with pytest.raises(ValidationError):
        received.artifacts[0].byte_size = 9


@pytest.mark.parametrize(
    "index,attribute,value",
    [
        (1, "id", OTHER),
        (1, "ledger_id", OTHER),
        (1, "created_by", OTHER),
        (1, "response_status", 200),
        (1, "response_status", 201.0),
        (1, "version", True),
        (1, "version", 2),
        (1, "zh_file_id", OTHER),
        (1, "en_file_id", OTHER),
        (2, "ledger_id", OTHER),
        (2, "created_by", OTHER),
        (3, "id", OTHER),
        (3, "detected_media_type", "image/png"),
        (3, "byte_size", True),
        (2, "byte_size", 0),
        (3, "sha256", "malformed"),
        (3, "sha256", "a" * 64),
        (3, "byte_size", 99),
        (0, "created_at", None),
        (1, "created_at", datetime(2026, 11, 13)),
        (1, "created_at", datetime(2026, 11, 12, tzinfo=UTC)),
    ],
)
def test_no_verified_completion_for_wrong_row_or_file_fact(index, attribute, value):
    rows = evidence()
    setattr(rows[index], attribute, value)
    with pytest.raises(MoneyError) as caught:
        read(*rows)
    assert caught.value.code == "invoice_completion_integrity"


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "short",
        "long",
        "reversed",
        "same-file",
        "same-file-both",
        "wrong-pdf",
        "normalized-id",
        "number",
        "snapshot",
        "bool-version",
        "float-min-digits",
        "true-size",
        "float-size",
        "nested-extra",
        "top-extra",
        "missing",
        "non-json-array",
    ],
)
def test_saved_response_must_match_all_immutable_json_facts(change):
    res, completed, zh, en = evidence()
    body = completed.response
    artifacts = body["artifacts"]
    if change == "pending":
        body["state"] = "pending"
    elif change == "short":
        artifacts.pop()
    elif change == "long":
        artifacts.append(artifacts[0])
    elif change == "reversed":
        artifacts.reverse()
    elif change in {"same-file", "same-file-both"}:
        artifacts[1].update(artifacts[0] | {"language": "en"})
        if change == "same-file-both":
            completed.en_file_id, en = zh.id, zh
    elif change == "wrong-pdf":
        artifacts[1]["sha256"] = artifacts[0]["sha256"]
    elif change == "normalized-id":
        body["id"] = body["id"].upper()
    elif change == "number":
        body["number"] += "9"
    elif change == "snapshot":
        body["snapshot_sha256"] = "a" * 64
    elif change == "bool-version":
        body["configuration_version"] = True
    elif change == "float-min-digits":
        body["numbering"]["minimum_digits"] = float(body["numbering"]["minimum_digits"])
    elif change in {"true-size", "float-size"}:
        artifacts[0]["byte_size"] = True if change == "true-size" else 1.0
    elif change == "nested-extra":
        artifacts[0]["download_url"] = "/fictional"
    elif change == "top-extra":
        body["extra"] = []
    elif change == "missing":
        body.pop("template_version")
    else:
        body["artifacts"] = tuple(artifacts)
    with pytest.raises(MoneyError) as caught:
        read(res, completed, zh, en)
    assert caught.value.code == "invoice_completion_integrity"


@pytest.mark.parametrize("which", ["missing-zh", "missing-en", "swapped", "missing-row"])
def test_no_verified_result_for_incomplete_pair(which):
    res, completed, zh, en = evidence()
    if which == "missing-zh":
        zh = None
    elif which == "missing-en":
        en = None
    elif which == "swapped":
        zh, en = en, zh
    else:
        completed = None
    with pytest.raises(MoneyError):
        read(res, completed, zh, en)


@pytest.mark.parametrize("scope", [{"owner": OTHER}, {"ledger": OTHER}])
def test_mismatched_reservation_rejected_before_using_its_completion(scope):
    with pytest.raises(MoneyError) as caught:
        read(*evidence(), **scope)
    assert caught.value.code == "invoice_reservation_integrity"
