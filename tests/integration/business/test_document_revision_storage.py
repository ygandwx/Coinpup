"""Persist fictional revisions with real transactions, journals and immutable receipts."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_hash import document_revision_hash_v2
from coinpup_api.business.document_fixed_receipts import replay_document_revision
from coinpup_api.business.document_revision_journals import prepare_document_revision_journals
from coinpup_api.business.document_revision_models import (
    BusinessDocumentAccrual,
    BusinessDocumentRevision,
    BusinessDocumentRevisionLine,
)
from coinpup_api.business.document_revision_references import reviewed_document_references
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.business.document_revision_storage import persist_document_revision
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.ledger.common import PostingCore, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation, Journal
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.integration.business.test_credit_document_origin import all_rows
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_references import pinned
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def write(s, document_id, command, key):
    core = PostingCore(s["engine"])
    with core._transaction(s["owner"], write=True, read_committed=True) as db:
        core._locked_ledger(db, s["owner"], s["ledger"])
        current = read_confirmed_document_version(db, s["ledger"], document_id)
        proposed = getattr(command, "replacement", None)
        asset_ids = {current.document.asset_id}
        if proposed is not None:
            asset_ids.add(proposed.asset_id)
            control = (
                "receivable.customer" if proposed.document_kind == "invoice" else "payable.supplier"
            )
            ControlAccounts(s["engine"]).ensure(
                db, s["owner"], s["ledger"], {control: {proposed.asset_id}}, lock_assets=asset_ids
            )
        catalog = {
            r.asset_id: asset_definition(r)
            for r in db.scalars(
                select(AssetRecord)
                .where(AssetRecord.asset_id.in_(asset_ids))
                .order_by(AssetRecord.asset_id)
                .with_for_update(read=True)
            )
        }
        refs = (
            reviewed_document_references(db, s["ledger"], current.document, proposed)
            if proposed
            else None
        )
        new_asset = catalog[proposed.asset_id] if proposed else None
        journals = prepare_document_revision_journals(
            db,
            current.document,
            command,
            current.postings,
            catalog[current.document.asset_id],
            new_asset,
        )
        action = "correct" if proposed else "cancel"
        raw = json.dumps(command.model_dump(mode="json", exclude_unset=True)).encode()
        digest = document_revision_hash_v2(action, s["ledger"], document_id, command, raw_body=raw)
        result = persist_document_revision(
            db, s["owner"], current, command, key, digest, refs, journals, new_asset
        )
    return result, digest


def reviewed(doc, rows, **values):
    proposed = replacement(doc, **values).model_copy(update={"lines": rows})
    refs = {("category", row.category_id, 1) for row in rows}
    refs.update(("project", row.project_id, 1) for row in rows if row.project_id)
    return pinned(proposed, sorted(refs, key=lambda r: (r[0], str(r[1]))))


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [4, 200])
def test_mixed_zero_lines_or_all_two_hundred_new_sources_commit_then_cancel_and_replay(
    drafts, kind, count
):
    s = drafts
    doc, original_postings = confirmed(s, kind, count)
    rows = replacement(doc).lines
    if count == 200:
        rows = [r.model_copy(update={"id": uuid4()}) for r in reversed(rows)]
    else:
        rows = [
            rows[0].model_copy(update={"unit_price": "-0.00"}),
            rows[1].model_copy(update={"unit_price": "0.02"}),
            rows[2].model_copy(update={"unit_price": "0.03"}),
            rows[3],
            rows[2].model_copy(update={"id": uuid4(), "unit_price": "0.04"}),
        ]
    command = ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional reviewed complete replacement",
        replacement=reviewed(doc, rows),
    )
    fixed, digest = write(s, doc.id, command, "fictional-storage-correct")
    assert [r.id for r in fixed.replacement.lines] == [r.id for r in rows]
    assert [r.unit_price for r in fixed.replacement.lines] == [r.unit_price for r in rows]
    with Session(s["engine"]) as db:
        active = read_confirmed_document_version(db, s["ledger"], doc.id)
        origin = read_confirmed_document_version(db, s["ledger"], doc.id, version=doc.version)
        assert origin.document == doc and origin.postings == original_postings
        assert active.document.total_amount == fixed.replacement.total_amount
        count_facts = db.scalars(
            select(BusinessDocumentRevisionLine).where(
                BusinessDocumentRevisionLine.revision_id == fixed.revision_id
            )
        ).all()
        assert len(count_facts) == len(rows)
        if count == 4:
            assert fixed.replacement.lines[0].posting is None
            assert fixed.replacement.lines[3].posting is None
            assert (
                fixed.replacement.lines[1].posting.operation_id == original_postings[1].operation_id
            )
            assert fixed.replacement.lines[2].posting.operation_version == 1
        else:
            assert set(p.operation_id for p in active.postings).isdisjoint(
                p.operation_id for p in original_postings
            )
    ending = ConfirmedDocumentCancellation(
        expected_version=fixed.version, reason="Fictional terminal group"
    )
    ended, end_digest = write(s, doc.id, ending, "fictional-storage-cancel")
    assert ended.previous == fixed.replacement and ended.replacement is None
    with s["engine"].begin() as c, Session(c) as db:
        before = all_rows(c)
        assert read_confirmed_document_version(db, s["ledger"], doc.id).document is None
        for action, payload, expected, key, hashed in (
            ("correct", command, fixed, "fictional-storage-correct", digest),
            ("cancel", ending, ended, "fictional-storage-cancel", end_digest),
        ):
            assert (
                replay_document_revision(
                    db,
                    db.get(CommandReceipt, (s["ledger"], key)),
                    s["ledger"],
                    doc.id,
                    action,
                    payload,
                    hashed,
                )
                == expected
            )
        assert all_rows(c) == before
        operation_ids = db.scalars(
            select(BusinessDocumentAccrual.operation_id).where(
                BusinessDocumentAccrual.document_id == doc.id
            )
        ).all()
        assert set(
            db.scalars(
                select(FinancialOperation.status).where(FinancialOperation.id.in_(operation_ids))
            )
        ) == {"cancelled"}
        assert all(
            db.scalars(select(Journal.sealed).where(Journal.operation_id.in_(operation_ids)))
        )


@pytest.mark.parametrize(
    "table",
    [
        "business_document_revision_lines",
        "journals",
        "business_document_accruals",
        "command_receipts",
    ],
)
def test_failure_at_each_persistence_boundary_rolls_back_every_table(drafts, table):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    command = ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional atomic storage failure",
        replacement=replacement(doc),
    )
    before = snapshot(s)

    def fail_after_write(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("INSERT INTO " + table + " "):
            raise RuntimeError("Fictional injected storage failure")

    event.listen(s["engine"], "after_cursor_execute", fail_after_write)
    try:
        with pytest.raises(RuntimeError, match="Fictional injected storage failure"):
            write(s, doc.id, command, "fictional-interrupted-storage")
    finally:
        event.remove(s["engine"], "after_cursor_execute", fail_after_write)
    assert snapshot(s) == before
    fixed, _ = write(s, doc.id, command, "fictional-interrupted-storage")
    with Session(s["engine"]) as db:
        assert db.get(BusinessDocumentRevision, fixed.revision_id) is not None


@pytest.mark.parametrize("price", ["0.020", "0.001"])
def test_excess_asset_precision_is_rejected_without_writing_any_table(drafts, price):
    from coinpup_api.ledger.errors import MoneyError

    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    proposed = replacement(doc, {doc.lines[0].id: {"unit_price": price}})
    command = ConfirmedDocumentCorrection(
        expected_version=doc.version, reason="Fictional excess precision", replacement=proposed
    )
    before = snapshot(s)
    with pytest.raises(MoneyError) as rejected:
        write(s, doc.id, command, "fictional-excess-price")
    assert rejected.value.code == "amount_precision"
    assert snapshot(s) == before


@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_asset_replacement_stores_minimum_units_and_keeps_original_usd_evidence(
    drafts, asset, unit, kind
):
    s = drafts
    doc, _ = confirmed(s, kind, 1)
    proposed = replacement(doc, {doc.lines[0].id: {"unit_price": unit}}, asset_id=asset)
    command = ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional minimum-unit asset replacement",
        replacement=proposed,
    )
    fixed, digest = write(s, doc.id, command, "fictional-minimum-unit")
    assert fixed.previous.asset_id == "USD" and fixed.previous.total_amount == "0.01"
    assert fixed.replacement.asset_id == asset and fixed.replacement.total_amount == unit
    ending = ConfirmedDocumentCancellation(
        expected_version=fixed.version, reason="Fictional asset end"
    )
    ended, _ = write(s, doc.id, ending, "fictional-minimum-end")
    assert ended.previous == fixed.replacement
    with s["engine"].begin() as c, Session(c) as db:
        before = all_rows(c)
        assert (
            replay_document_revision(
                db,
                db.get(CommandReceipt, (s["ledger"], "fictional-minimum-unit")),
                s["ledger"],
                doc.id,
                "correct",
                command,
                digest,
            )
            == fixed
        )
        assert all_rows(c) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_zero_to_positive_starts_new_operation_without_reviving_retired_history(drafts, kind):
    s = drafts
    doc, originals = confirmed(s, kind, 4)
    first = ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional zero first source",
        replacement=replacement(doc, {doc.lines[0].id: {"unit_price": "0"}}),
    )
    fixed, _ = write(s, doc.id, first, "fictional-zero-source")
    with Session(s["engine"]) as db:
        active = read_confirmed_document_version(db, s["ledger"], doc.id).document
    second = ConfirmedDocumentCorrection(
        expected_version=fixed.version,
        reason="Fictional fresh positive source",
        replacement=replacement(active, {doc.lines[0].id: {"unit_price": "0.03"}}),
    )
    newer, _ = write(s, doc.id, second, "fictional-new-accrual")
    fresh = newer.replacement.lines[0].posting
    assert fresh.operation_version == 1 and fresh.operation_id != originals[0].operation_id
    with Session(s["engine"]) as db:
        old = db.get(FinancialOperation, originals[0].operation_id)
        assert old.status == "cancelled" and old.version == 2
        assert db.get(FinancialOperation, fresh.operation_id).status == "active"
        assert (
            read_confirmed_document_version(db, s["ledger"], doc.id, version=doc.version).document
            == doc
        )
