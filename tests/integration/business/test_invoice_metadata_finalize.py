"""Atomic metadata seal with the original reservation gate still closed."""

from dataclasses import replace
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_file_choices import _select_choices, _verify_selected_bytes
from coinpup_api.business.invoice_metadata_finalize import InvoiceFinalization, _seal
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.models import Entity
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_invoice_file_choices import canonical, reservation
from tests.integration.business.test_invoice_file_choices import originals as originals
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_invoice_snapshot_binding import company as company
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def choices_for(s, res, store, pair):
    with reservation(s, res) as session:
        result = _select_choices(session, s["owner"], s["ledger"], res["id"], pair)
    _verify_selected_bytes(store, result)
    return result


def sealing(s, session, res, pair, choices):
    _, entity = InvoiceFinalization(s["engine"])._locked_ledger(session, s["owner"], s["ledger"])
    return _seal(
        session,
        owner=s["owner"],
        ledger=s["ledger"],
        entity=entity,
        identifier=res["id"],
        prepared=pair,
        verified=choices,
    )


@pytest.mark.parametrize("canonical_languages", [(), ("zh",), ("zh", "en")])
def test_metadata_pair_response_and_notifications_seal_together_and_replay_exactly(
    originals, canonical_languages
):
    s, res, store, data, pair = originals
    prior = {}
    for blob, content in zip(pair.artifacts, data, strict=True):
        if blob.language in canonical_languages:
            _, prior[blob.language] = canonical(s, store, blob.language, content)
    verified = choices_for(s, res, store, pair)
    before = snapshot(s)
    with reservation(s, res) as session:
        session.execute(
            update(Entity).where(Entity.kind == "company").values(archived=True, version=2)
        )
        response = sealing(s, session, res, pair, verified)
        completion = session.get(InvoiceCompletion, res["id"])
        assert completion.response_status == 201 and completion.response == response.model_dump(
            mode="json"
        )
        assert tuple(a.language for a in response.artifacts) == ("zh", "en")
        for choice, artifact in zip(verified.artifacts, response.artifacts, strict=True):
            row = session.get(StoredFile, artifact.file_id)
            assert (row.ledger_id, row.created_by, row.blob_key) == (
                s["ledger"],
                s["owner"],
                choice.blob_key,
            )
            assert (row.sha256, row.byte_size, row.detected_media_type) == (
                artifact.sha256,
                artifact.byte_size,
                "application/pdf",
            )
            if choice.language in prior:
                assert row.id == prior[choice.language]
            else:
                assert row.original_filename == f"invoice-{res['id']}-{choice.language}.pdf"
        events = session.execute(
            select(ChangeLog.entity_id, ChangeLog.entity_version).where(
                ChangeLog.entity_type == "business_invoice_completions"
            )
        ).all()
        assert events == [(str(res["id"]), 1)]
        assert sealing(s, session, res, None, None) == response
        assert (
            session.execute(
                select(ChangeLog.entity_id, ChangeLog.entity_version).where(
                    ChangeLog.entity_type == "business_invoice_completions"
                )
            ).all()
            == events
        )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "change", ["racing-canonical", "wrong-owner", "wrong-ledger", "wrong-id", "snapshot", "swapped"]
)
def test_changed_choices_never_seal_unverified_metadata(originals, change):
    s, res, store, data, pair = originals
    verified = choices_for(s, res, store, pair)
    if change == "racing-canonical":
        canonical(s, store, "en", data[1])
    elif change == "swapped":
        verified = replace(verified, artifacts=tuple(reversed(verified.artifacts)))
    else:
        key = {
            "wrong-owner": "owner_id",
            "wrong-ledger": "ledger_id",
            "wrong-id": "issuance_id",
            "snapshot": "snapshot_sha256",
        }[change]
        verified = replace(verified, **{key: "f" * 64 if change == "snapshot" else uuid4()})
    before = snapshot(s)
    with reservation(s, res) as session, pytest.raises(LedgerError) as rejected:
        sealing(s, session, res, pair, verified)
    assert (rejected.value.code, rejected.value.status) == ("invoice_file_choice_changed", 409)
    assert snapshot(s) == before


@pytest.mark.parametrize("stop", ["files", "seal"])
def test_late_metadata_failure_rolls_back_both_files_completion_and_notifications(originals, stop):
    s, res, store, _, pair = originals
    verified = choices_for(s, res, store, pair)
    before = snapshot(s)

    class Failure(Exception):
        pass

    def stop_after(conn, cursor, stmt, params, context, many):
        table = "stored_files" if stop == "files" else "business_invoice_completions"
        if stmt.startswith("INSERT INTO " + table + " "):
            raise Failure

    with pytest.raises(Failure), s["engine"].begin() as conn:
        conn.execute(insert(InvoiceReservation).values(**res))
        event.listen(conn, "after_cursor_execute", stop_after)
        try:
            with Session(bind=conn, join_transaction_mode="rollback_only") as session:
                sealing(s, session, res, pair, verified)
        finally:
            event.remove(conn, "after_cursor_execute", stop_after)
    assert snapshot(s) == before
    _verify_selected_bytes(store, verified)


def test_even_two_sealed_rows_do_not_bypass_the_closed_reservation_commit(originals):
    s, res, store, _, pair = originals
    verified = choices_for(s, res, store, pair)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**res))
        with Session(bind=c, join_transaction_mode="rollback_only") as session:
            assert sealing(s, session, res, pair, verified).state == "complete"
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_complete"
    assert snapshot(s) == before


def test_public_method_rejects_absent_owned_row_and_never_writes_metadata(originals):
    s, res, _, _, pair = originals
    before = snapshot(s)
    with pytest.raises(LedgerError) as missing:
        InvoiceFinalization(s["engine"]).seal(
            s["owner"], s["ledger"], res["id"], prepared=pair, verified=None
        )
    assert missing.value.status == 404 and snapshot(s) == before
