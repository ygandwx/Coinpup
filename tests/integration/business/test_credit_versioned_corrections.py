"""Credit corrections use current document capacity and keep each immutable source binding."""

from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
)
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from tests.integration.business.test_credit_revision_boundaries import race
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_credit_versioned_create import current, request
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_storage import reviewed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def command(doc, amount="0.02", *, version=1, sources=None):
    proposed = request(doc, amount, sources=sources)
    return CreditCorrectionCreate(
        expected_version=version,
        reason="Fictional credit correction",
        replacement=proposed.model_dump(include={"credit_date", "reason", "lines"}),
    )


def revise(s, identifier, payload, key="fictional-current-correction"):
    method = "correct" if isinstance(payload, CreditCorrectionCreate) else "cancel"
    return getattr(BusinessCreditRevisionService(s["engine"]), method)(
        s["owner"], s["ledger"], identifier, payload, key
    )


def expanded(s, kind="invoice", count=1):
    doc, _ = confirmed(s, kind, count)
    original = request(doc)
    created = credit(s, original)
    change = correction(doc).model_copy(
        update={"replacement": replacement(doc, {r.id: {"unit_price": "0.02"} for r in doc.lines})}
    )
    fixed = send(s, doc, change, "fictional-expanded-current-source")
    return doc, original, created, fixed, current(s, doc).document


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_current_capacity_replaces_entire_group_and_old_201_and_each_200_never_change(
    drafts, kind, count
):
    s = drafts
    doc, original, created, source, active = expanded(s, kind, count)
    payload = command(active)
    corrected = revise(s, created.id, payload)
    assert corrected.version == 2 and len(corrected.members) == count
    assert all(
        m.action == "correct" and Decimal(m.total_amount) == Decimal("0.02")
        for m in corrected.members
    )
    with Session(s["engine"]) as db:
        assert db.get(BusinessCredit, created.id).document_revision_id is None
        assert (
            db.get(BusinessCreditRevision, corrected.revision_id).document_revision_id
            == source.revision_id
        )
        assert all(
            total == 0
            for _, total in db.execute(
                select(JournalLine.asset_id, func.sum(JournalLine.amount)).group_by(
                    JournalLine.asset_id
                )
            )
        )
    ending = CreditCancellationCreate(expected_version=2, reason="Fictional explicit release")
    ended = revise(s, created.id, ending, "fictional-current-credit-end")
    send(
        s,
        doc,
        ConfirmedDocumentCancellation(
            expected_version=source.version, reason="Fictional source end"
        ),
        "fictional-corrected-source-end",
    )
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert credit(s, original) == created
    assert revise(s, created.id, payload) == corrected
    assert revise(s, created.id, ending, "fictional-current-credit-end") == ended
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
def test_credit_created_on_revised_asset_can_correct_then_cancel_in_its_actual_units(
    drafts, kind, asset, unit
):
    s = drafts
    doc, _ = confirmed(s, kind, 1)
    source = send(
        s,
        doc,
        correction(doc).model_copy(
            update={
                "replacement": replacement(
                    doc, {doc.lines[0].id: {"unit_price": unit}}, asset_id=asset
                )
            }
        ),
        "fictional-asset-source",
    )
    active = current(s, doc).document
    original = request(active, unit)
    created = credit(s, original)
    payload = command(active, unit)
    corrected = revise(s, created.id, payload)
    ended = revise(
        s,
        created.id,
        CreditCancellationCreate(expected_version=2, reason="Fictional asset credit end"),
        "fictional-asset-credit-end",
    )
    assert created.asset_id == corrected.asset_id == ended.asset_id == asset
    assert Decimal(corrected.members[0].total_amount) == Decimal(unit)
    with Session(s["engine"]) as db:
        assert db.get(BusinessCredit, created.id).document_revision_id == source.revision_id
        assert (
            db.get(BusinessCreditRevision, corrected.revision_id).document_revision_id
            == source.revision_id
        )
    before = snapshot(s)
    assert credit(s, original) == created and revise(s, created.id, payload) == corrected
    assert snapshot(s) == before


def test_explicit_credit_source_replacement_releases_the_old_document_line(drafts):
    s = drafts
    doc, original, created, _, active = expanded(s)
    (old,) = replacement(active).lines
    new = old.model_copy(update={"id": uuid4()})
    added = send(
        s,
        doc,
        correction(active).model_copy(update={"replacement": reviewed(active, [old, new])}),
        "fictional-add-new-source",
    )
    active = current(s, doc).document
    payload = command(active, sources=[active.lines[1]])
    corrected = revise(s, created.id, payload)
    assert {m.action for m in corrected.members} == {"cancel", "create"}
    removed = send(
        s,
        doc,
        correction(active).model_copy(update={"replacement": replacement(active, {old.id: None})}),
        "fictional-remove-released-source",
    )
    assert removed.version == added.version + 1
    before = snapshot(s)
    assert credit(s, original) == created and revise(s, created.id, payload) == corrected
    assert snapshot(s) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_real_concurrent_corrections_on_a_revised_document_commit_one_group(drafts, same_key):
    s = drafts
    _, _, created, _, active = expanded(s)
    payload = command(active)
    results = race(
        s,
        [
            lambda n=n: revise(
                s,
                created.id,
                payload,
                "fictional-shared-current" if same_key else f"fictional-current-{n}",
            )
            for n in (0, 1)
        ],
    )
    successful = [r for r in results if not isinstance(r, LedgerError)]
    assert len(successful) == (2 if same_key else 1)
    if same_key:
        assert successful[0] == successful[1]
    else:
        assert [r.code for r in results if isinstance(r, LedgerError)] == ["version_conflict"]


@pytest.mark.parametrize(
    "fault,code",
    [
        ("capacity", "credit_exceeds_source"),
        ("precision", "amount_precision"),
        ("stale", "version_conflict"),
        ("closed", "period_closed"),
    ],
)
def test_revised_source_rejections_preserve_all_tables(drafts, fault, code):
    s = drafts
    _, _, created, _, active = expanded(s)
    payload = command(
        active,
        "0.03" if fault == "capacity" else "0.001" if fault == "precision" else "0.02",
        version=2 if fault == "stale" else 1,
    )
    if fault == "closed":
        change_period(s, cutoff="2026-11-06")
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        revise(s, created.id, payload)
    assert rejected.value.code == code and snapshot(s) == before


def test_receipt_failure_rolls_back_new_member_heads_and_source_bindings(drafts):
    s = drafts
    _, _, created, _, active = expanded(s)
    payload = command(active)
    before = snapshot(s)

    def fail(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("INSERT INTO command_receipts "):
            raise RuntimeError("Fictional correction receipt failure")

    event.listen(s["engine"], "after_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="Fictional correction receipt failure"):
            revise(s, created.id, payload)
    finally:
        event.remove(s["engine"], "after_cursor_execute", fail)
    assert snapshot(s) == before
    fixed = revise(s, created.id, payload)
    before = snapshot(s)
    assert revise(s, created.id, payload) == fixed and snapshot(s) == before
