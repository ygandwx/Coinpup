"""A pending issuance always uses its frozen bytes and original private Logo."""

import asyncio
import base64
import hashlib
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_render_sources import (
    InvoiceRenderSources,
    _read_original,
    _source_metadata,
)
from coinpup_api.files.models import StoredFile
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import update

from tests.integration.business import test_invoice_snapshot_binding as binding
from tests.integration.business.test_invoice_file_choices import reservation
from tests.integration.business.test_invoice_profile_storage import file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMQdtvyHwADigINTnI7cwAAAABJRU5ErkJggg=="
)


@pytest.fixture
def pending(company, tmp_path, monkeypatch):
    s = company
    store = FileStore(tmp_path / "private", 1024, 5)

    async def publish():
        async def chunks():
            yield PNG

        stage = await store.stage(chunks(), len(PNG))
        return store.publish(stage)

    key = asyncio.run(publish())
    identifier = file(s, sha256=hashlib.sha256(PNG).hexdigest(), byte_size=len(PNG), blob_key=key)
    monkeypatch.setattr(binding, "file", lambda scope: identifier)
    res = binding.reviewed(s, logo=True, uppercase=True)
    return s, res, store, key


def test_saved_logo_after_archive_and_profile_edit_is_the_original_source(pending):
    s, res, store, _ = pending
    before = snapshot(s)
    with reservation(s, res) as session:
        session.execute(
            update(StoredFile)
            .where(StoredFile.id == res["logo_file_id"])
            .values(archived=True, title="Fictional changed", version=2)
        )
        session.execute(
            update(InvoiceProfile)
            .where(InvoiceProfile.id == res["configuration_id"])
            .values(archived=True, version=2)
        )
        frozen, saved = _source_metadata(session, s["owner"], s["ledger"], res["id"])
    assert frozen.content.decode() == res["snapshot_json"] and saved.identity.reviewed_version == 1
    assert saved.blob_key not in repr(saved)
    assert _read_original(store, saved) == PNG
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["missing", "tampered", "truncated"])
def test_lost_original_bytes_are_never_replaced_with_a_current_logo(pending, fault):
    s, res, store, key = pending
    with reservation(s, res) as session:
        _, saved = _source_metadata(session, s["owner"], s["ledger"], res["id"])
    path = store.root / "blobs" / key
    if fault == "missing":
        path.unlink()
    elif fault == "tampered":
        path.write_bytes(PNG[:-1] + b"X")
    else:
        path.write_bytes(PNG[:-5])
    before = snapshot(s)
    with pytest.raises(LedgerError) as caught:
        _read_original(store, saved)
    assert (caught.value.code, caught.value.status) == ("invoice_logo_integrity", 503)
    assert snapshot(s) == before


def test_no_logo_never_accesses_blob_storage(company):
    s = company
    res = binding.reviewed(s)
    with reservation(s, res) as session:
        frozen, saved = _source_metadata(session, s["owner"], s["ledger"], res["id"])
    assert saved is None and _read_original(None, saved) is None and frozen.decode()["logo"] is None


@pytest.mark.parametrize("scope", ["wrong-owner", "personal", "unknown-ledger", "missing-intent"])
def test_scope_rejection_never_opens_a_private_blob(company, scope):
    s = company
    ledger = (
        s["entity"].ledger.id
        if scope == "personal"
        else uuid4()
        if scope == "unknown-ledger"
        else s["ledger"]
    )
    with pytest.raises(LedgerError) as caught:
        InvoiceRenderSources(s["engine"]).load(
            uuid4() if scope == "wrong-owner" else s["owner"], ledger, uuid4(), store=None
        )
    assert caught.value.code == ("invoice_requires_company" if scope == "personal" else "not_found")
