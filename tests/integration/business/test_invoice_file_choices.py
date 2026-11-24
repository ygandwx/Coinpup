"""Native private files plus actual uncommitted PG17 rows; no manufactured issuance commit."""

import asyncio
import hashlib
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_artifacts import PreparedInvoiceBlob, PreparedInvoiceBlobs
from coinpup_api.business.invoice_file_choices import (
    InvoiceOriginalFiles,
    _select_choices,
    _verify_selected_bytes,
)
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import insert, update
from sqlalchemy.orm import Session

from tests.integration.business.test_invoice_profile_storage import file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


async def publish(store, lang, data):
    async def chunks():
        yield data

    staged = await store.stage(chunks(), len(data))
    key = store.publish(staged)
    return PreparedInvoiceBlob(lang, key, hashlib.sha256(data).hexdigest(), len(data))


@pytest.fixture
def originals(company, tmp_path):
    s = company
    res = reviewed(s)
    store = FileStore(tmp_path / "private", 100000, 5)
    # File identity/staging fixtures; full rendering/visual checks belong to the invoice suite.
    data = [b"%PDF-1.4\nfictional-" + lang.encode() + b"\n%%EOF\n" for lang in ("zh", "en")]
    blobs = [
        asyncio.run(publish(store, lang, value))
        for lang, value in zip(("zh", "en"), data, strict=True)
    ]
    return s, res, store, data, PreparedInvoiceBlobs(res["snapshot_sha256"], tuple(blobs))


def canonical(s, store, lang, data, **changes):
    blob = asyncio.run(publish(store, lang, data))
    identifier = file(
        s,
        **(
            dict(
                blob_key=blob.blob_key,
                sha256=blob.sha256,
                byte_size=blob.byte_size,
                detected_media_type="application/pdf",
            )
            | changes
        ),
    )
    return blob, identifier


@contextmanager
def reservation(s, res):
    with s["engine"].connect() as c, c.begin() as tx:
        c.execute(insert(InvoiceReservation).values(**res))
        with Session(bind=c, join_transaction_mode="rollback_only") as session:
            yield session
        tx.rollback()


@pytest.mark.parametrize("existing", [(), ("zh",), ("en",), ("zh", "en")])
def test_canonical_or_fresh_originals_are_chosen_per_language_and_verified_after_transaction(
    originals, existing
):
    s, res, store, data, pair = originals
    chosen = {}
    for item, raw in zip(pair.artifacts, data, strict=True):
        if item.language in existing:
            chosen[item.language] = canonical(s, store, item.language, raw)
    before = snapshot(s)
    with reservation(s, res) as session:
        choices = _select_choices(session, s["owner"], s["ledger"], res["id"], pair)
    assert choices.snapshot_sha256 == res["snapshot_sha256"] and choices.issuance_id == res["id"]
    for choice, item in zip(choices.artifacts, pair.artifacts, strict=True):
        assert choice.language == item.language and choice.sha256 == item.sha256
        if choice.language in chosen:
            duplicate, identifier = chosen[item.language]
            assert choice.blob_key == duplicate.blob_key != item.blob_key
            assert choice.existing_file_id == identifier
        else:
            assert choice.blob_key == item.blob_key and choice.existing_file_id is None
        assert choice.blob_key not in repr(choices)
    _verify_selected_bytes(store, choices)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "snapshot",
        "one",
        "reverse",
        "size",
        "digest",
        "same",
        "key",
        "oversized",
        "archived",
        "wrong-media",
    ],
)
def test_invalid_pair_or_invalid_canonical_metadata_never_yields_a_choice(originals, fault):
    s, res, store, data, pair = originals
    zh, en = pair.artifacts
    if fault == "snapshot":
        pair = replace(pair, snapshot_sha256="a" * 64)
    elif fault == "one":
        pair = replace(pair, artifacts=(zh,))
    elif fault == "reverse":
        pair = replace(pair, artifacts=(en, zh))
    elif fault == "same":
        pair = replace(pair, artifacts=(zh, replace(zh, language="en", blob_key=en.blob_key)))
    elif fault == "archived":
        _, identifier = canonical(s, store, "zh", data[0])
        with s["engine"].begin() as c:
            c.execute(
                update(StoredFile)
                .where(StoredFile.id == identifier)
                .values(archived=True, version=2)
            )
    elif fault == "wrong-media":
        canonical(s, store, "zh", data[0], detected_media_type="image/png")
    else:
        changes = {
            "size": {"byte_size": True},
            "oversized": {"byte_size": 8 * 1024 * 1024 + 1},
            "digest": {"sha256": "Z" * 64},
            "key": {"blob_key": "?" * 32},
        }[fault]
        pair = replace(pair, artifacts=(replace(zh, **changes), en))
    before = snapshot(s)
    with reservation(s, res) as session, pytest.raises(LedgerError) as rejected:
        _select_choices(session, s["owner"], s["ledger"], res["id"], pair)
    assert rejected.value.code == (
        "file_archived" if fault == "archived" else "invoice_artifact_integrity"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_valid_new_copy_cannot_hide_corrupt_or_missing_canonical_blob(originals, fault):
    s, res, store, data, pair = originals
    existing, _ = canonical(s, store, "en", data[1])
    before = snapshot(s)
    with reservation(s, res) as session:
        choices = _select_choices(session, s["owner"], s["ledger"], res["id"], pair)
    path = store.root / "blobs" / existing.blob_key
    if fault == "missing":
        path.unlink()
    else:
        path.write_bytes(b"X" * len(data[1]))
    assert (store.root / "blobs" / pair.artifacts[1].blob_key).read_bytes() == data[1]
    with pytest.raises(LedgerError) as rejected:
        _verify_selected_bytes(store, choices)
    assert rejected.value.code == "invoice_artifact_integrity" and snapshot(s) == before


def test_unauthorized_or_absent_issuance_never_opens_private_storage(originals):
    s, res, _, _, pair = originals
    service = InvoiceOriginalFiles(s["engine"])
    before = snapshot(s)
    for owner, ledger, code in [
        (uuid4(), s["ledger"], "not_found"),
        (s["owner"], s["entity"].ledger.id, "invoice_requires_company"),
        (s["owner"], s["ledger"], "not_found"),
    ]:
        with pytest.raises(LedgerError) as rejected:
            service.select_verified(owner, ledger, res["id"], pair, store=None)
        assert rejected.value.code == code
    assert snapshot(s) == before
