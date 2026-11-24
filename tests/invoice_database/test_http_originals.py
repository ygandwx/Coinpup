"""Verify a real authorized HTTP receipt and authenticated original downloads."""

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.business.test_invoice_issuance_http import path, post
from tests.integration.business.test_invoice_predecessor import advance, next_input
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.business.test_invoice_snapshot_binding import reviewed
from tests.integration.conftest import authenticated_client as authenticated_client
from tests.integration.conftest import structure_database as structure_database
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
UPLOAD_LIMIT = 1024


@pytest.fixture
def scoped_store(tmp_path, monkeypatch):
    monkeypatch.setenv("COINPUP_FILES_DIRECTORY", str(tmp_path / "private"))
    monkeypatch.setenv("COINPUP_MAX_UPLOAD_BYTES", str(UPLOAD_LIMIT))
    assert Path(os.environ["COINPUP_INVOICE_FONT_DIR"]).is_dir()


def test_corrected_invoice_new_http_receipt_and_all_original_downloads_stay_private_and_fixed(
    scoped_store, authenticated_client, company
):
    s = company
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    row = reviewed(s, 200, uppercase=True)
    configuration = client.get("/api/v1/files/configuration")
    assert configuration.status_code == 200
    assert configuration.json()["max_upload_bytes"] == UPLOAD_LIMIT
    oversized_upload = client.post(
        f"/api/v1/ledgers/{s['ledger']}/uploads",
        json={
            "id": str(uuid4()),
            "original_filename": "fictional.pdf",
            "declared_size": UPLOAD_LIMIT + 1,
        },
    )
    assert oversized_upload.status_code == 413
    assert oversized_upload.json()["detail"]["code"] == "file_too_large"

    url = path(s["ledger"])
    first = post(client, url, row)
    assert first.status_code == 201, first.text
    assert first.headers["cache-control"] == "no-store"
    old = first.json()
    assert old["state"] == "complete" and [i["language"] for i in old["artifacts"]] == ["zh", "en"]

    def download(response):
        data = {}
        for artifact in response["artifacts"]:
            file = client.get(f"/api/v1/ledgers/{s['ledger']}/files/{artifact['file_id']}/content")
            assert file.status_code == 200 and file.headers["cache-control"] == "no-store"
            assert file.headers["content-type"] == "application/pdf" and file.content.startswith(
                b"%PDF-"
            )
            assert (
                UPLOAD_LIMIT < len(file.content) == artifact["byte_size"]
                and hashlib.sha256(file.content).hexdigest() == artifact["sha256"]
            )
            data[artifact["language"]] = file.content
        return data

    old_bytes = download(old)
    advance(s, row["document_id"])
    successor = next_input(s, row, 2)
    new = post(client, url, successor)
    assert new.status_code == 201
    updated = new.json()
    assert updated["previous_issuance_id"] == old["id"] and updated["number"] != old["number"]
    before = snapshot(s)
    assert (
        post(client, url, row).content == first.content
        and post(client, url, successor).content == new.content
    )
    for receipt in (old, updated):
        got = client.get(url + "/" + receipt["id"])
        assert got.status_code == 200 and got.json() == receipt
    assert download(old) == old_bytes and all(
        a != old_bytes[k] for k, a in download(updated).items()
    )
    sample = old["artifacts"][0]["file_id"]
    assert client.get(f"/api/v1/ledgers/{uuid4()}/files/{sample}/content").status_code == 404
    client.cookies.clear()
    assert client.get(f"/api/v1/ledgers/{s['ledger']}/files/{sample}/content").status_code == 401
    assert snapshot(s) == before
