"""Whole empty-target bundle of reviewed logo, sealed bilingual originals, and pending reissue."""

import asyncio
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_issuance_reads import InvoiceIssuanceReads
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_issuance_service import InvoiceIssuanceService
from coinpup_api.business.invoice_reservation_command import InvoiceReservationCommand
from coinpup_api.files.storage import FileStore
from psycopg import sql
from sqlalchemy import create_engine

from tests.integration.business.test_invoice_predecessor import advance, current
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_render_sources import PNG
from tests.integration.business.test_invoice_render_sources import pending as pending
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.conftest import structure_database as structure_database
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.invoice_database.test_committed_issuance import issue, private_pair


def corrected(s, row, previous, sequence):
    facts = current(s, row["document_id"])
    cmd = InvoiceIssuanceCreate(
        id=uuid4(),
        document_id=row["document_id"],
        expected_document_version=facts.version,
        configuration_id=row["configuration_id"],
        expected_configuration_version=1,
        previous_issuance_id=previous,
    )
    return dict(
        owner=s["owner"],
        ledger=s["ledger"],
        command=cmd,
        raw_body=cmd.model_dump_json().encode(),
        key="fictional-restored-invoice-" + str(sequence),
    )


@pytest.mark.integration
def test_bundle_keeps_all_tables_logo_both_originals_and_pending_retry_with_next_number(
    pending, tmp_path
):
    from _database_archive import read_target
    from backup_bundle import backup_bundle
    from check_backup_restore import change_sequence_state, change_state, snapshot
    from restore_bundle import restore_bundle

    s, row, logo_store, logo_key = pending
    source = read_target("COINPUP_DATABASE_URL")
    assert source.host in ("127.0.0.1", "localhost") and source.database == "coinpup"
    target = replace(source, database="coinpup_restore_invoice_" + uuid4().hex)
    with source.connect(autocommit=True) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target.database))
        )
    store = FileStore(logo_store.root, 8 * 1024 * 1024, 20)
    fonts = Path(os.environ["COINPUP_INVOICE_FONT_DIR"])
    first = issue(s, row, store, fonts)
    first_bytes = private_pair(s, store, first)
    advance(s, row["document_id"])
    second_cmd = corrected(s, row, first.id, 2)
    second = InvoiceReservationCommand(s["engine"]).reserve(**second_cmd)
    assert (
        second.state == "pending"
        and second.sequence == "2"
        and second.previous_issuance_id == first.id
    )
    before = snapshot(source)
    cursor = change_state(s["engine"], s["owner"])
    sequence = change_sequence_state(source)
    assert len(before["stored_files"]) == 3 and len(before["business_invoice_completions"]) == 1
    directory = tmp_path / "bundle"
    backup_bundle(source, store.root, directory)
    dest = tmp_path / "restored-originals"
    restore_bundle(target, directory, dest, target.database)
    engine = create_engine(s["engine"].url.set(database=target.database), hide_parameters=True)
    try:
        assert snapshot(target) == before and change_sequence_state(target) == sequence
        assert change_state(engine, s["owner"]) == cursor
        new_s = s | {"engine": engine}
        restored_store = FileStore(dest, 8 * 1024 * 1024, 20)
        with restored_store.open_blob(logo_key, hashlib.sha256(PNG).hexdigest(), len(PNG)) as file:
            assert file.read() == PNG
        assert issue(new_s, row, restored_store, tmp_path / "no-rerender") == first
        assert private_pair(new_s, restored_store, first) == first_bytes
        assert InvoiceIssuanceReads(engine).get(s["owner"], s["ledger"], second.id) == second
        assert InvoiceReservationCommand(engine).reserve(**second_cmd) == second
        assert snapshot(target) == before and change_sequence_state(target) == sequence
        svc = InvoiceIssuanceService(engine, store=restored_store, font_directory=fonts)
        done = asyncio.run(svc.issue(**second_cmd))
        assert done.state == "complete"
        assert (done.id, done.number, done.snapshot_sha256) == (
            second.id,
            second.number,
            second.snapshot_sha256,
        )
        second_bytes = private_pair(new_s, restored_store, done)
        assert all(second_bytes[k] != first_bytes[k] for k in first_bytes)
        advance(new_s, row["document_id"])
        third = InvoiceReservationCommand(engine).reserve(**corrected(new_s, row, done.id, 3))
        assert third.sequence == "3" and third.number not in (first.number, done.number)
        continued = snapshot(target)
        assert len(continued["business_invoice_reservations"]) == 3
        assert (
            len(continued["business_invoice_completions"]) == 2
            and len(continued["stored_files"]) == 5
        )
        assert issue(new_s, row, restored_store, tmp_path / "no-rerender") == first
        assert asyncio.run(svc.issue(**second_cmd)) == done
        assert private_pair(new_s, restored_store, first) == first_bytes
        assert snapshot(target) == continued and snapshot(source) == before
        print(
            f"Verified all {len(before)} SQL tables, cursor and sequence; 3 original blobs; "
            "original receipt zero-write replay, pending #2 completion and continued #3."
        )
    finally:
        engine.dispose()
