"""Authorized synthetic document commands replay before current availability checks."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_schemas import CreditCancellationCreate
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.document_revision_models import BusinessDocumentRevision
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.business.document_revisions import BusinessDocumentRevisionService
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.integration.business.test_credit_revision_boundaries import race
from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def correction(doc, **changes):
    return ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional complete correction 中文",
        replacement=replacement(doc, **changes),
    )


def send(s, doc, payload, key, *, owner=None, ledger=None, raw=None):
    method = "correct" if isinstance(payload, ConfirmedDocumentCorrection) else "cancel"
    return getattr(BusinessDocumentRevisionService(s["engine"]), method)(
        owner or s["owner"], ledger or s["ledger"], doc.id, payload, key, raw_body=raw
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_fixed_intents_replay_after_cancel_closed_period_disabled_asset_and_archive(
    drafts, kind, count
):
    s = drafts
    doc, _ = confirmed(s, kind, count)
    command = correction(doc, notes="Fictional revised note")
    raw = json.dumps(command.model_dump(mode="json", exclude_unset=True)).encode()
    fixed = send(s, doc, command, "fictional-document-correct", raw=raw)
    ending = ConfirmedDocumentCancellation(expected_version=fixed.version, reason="Fictional end")
    ended = send(s, doc, ending, "fictional-document-cancel")
    assert fixed.version == doc.version + 1 and ended.version == doc.version + 2
    assert len(ended.reversals) == count
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert send(s, doc, command, "fictional-document-correct", raw=raw) == fixed
    assert send(s, doc, ending, "fictional-document-cancel") == ended
    with pytest.raises(LedgerError) as conflict:
        send(
            s,
            doc,
            command.model_copy(update={"reason": "Fictional different intent"}),
            "fictional-document-correct",
        )
    assert conflict.value.code == "idempotency_conflict"
    assert snapshot(s) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_same_intent_or_same_version_has_one_committed_group(drafts, same_key):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    command = correction(doc)
    results = race(
        s,
        [
            lambda n=n: send(
                s, doc, command, "fictional-race" if same_key else f"fictional-race-{n}"
            )
            for n in (0, 1)
        ],
    )
    good = [r for r in results if not isinstance(r, LedgerError)]
    if same_key:
        assert len(good) == 2 and good[0] == good[1]
    else:
        assert len(good) == 1
        assert [r.code for r in results if isinstance(r, LedgerError)] == ["version_conflict"]
    with s["engine"].connect() as c:
        assert c.scalar(select(func.count()).select_from(BusinessDocumentRevision)) == 1
        assert (
            c.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.response_status == 200)
            )
            == 1
        )


@pytest.mark.parametrize(
    "fault,code",
    [
        ("owner", "not_found"),
        ("ledger", "not_found"),
        ("version", "version_conflict"),
        ("key", "invalid_idempotency_key"),
        ("period", "period_closed"),
        ("archive", "entity_archived"),
        ("precision", "amount_precision"),
    ],
)
def test_invalid_new_command_changes_no_table(drafts, fault, code):
    s = drafts
    doc, _ = confirmed(s, "invoice", 4)
    command = correction(doc)
    args = {}
    key = "fictional-invalid-document"
    if fault == "owner":
        args["owner"] = uuid4()
    if fault == "ledger":
        args["ledger"] = uuid4()
    if fault == "version":
        command = command.model_copy(update={"expected_version": doc.version + 1})
    if fault == "key":
        key = "bad key"
    if fault == "period":
        change_period(s, cutoff="2026-11-06")
    if fault == "archive":
        s["structure"].update_entity(
            s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
        )
    if fault == "precision":
        command = command.model_copy(
            update={"replacement": replacement(doc, {doc.lines[0].id: {"unit_price": "0.001"}})}
        )
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        send(s, doc, command, key, **args)
    assert rejected.value.code == code
    assert snapshot(s) == before


def test_live_credit_requires_explicit_cancellation_before_document_cancel(drafts):
    s = drafts
    saved, _, _, request = setup_credit(s)
    original = credit(s, request)
    with Session(s["engine"]) as db:
        doc = read_confirmed_document_version(db, s["ledger"], saved.id).document
    ending = ConfirmedDocumentCancellation(
        expected_version=doc.version, reason="Fictional remove document"
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        send(s, doc, ending, "fictional-end-with-credit")
    assert rejected.value.code == "document_revision_dependency"
    assert snapshot(s) == before
    BusinessCreditRevisionService(s["engine"]).cancel(
        s["owner"],
        s["ledger"],
        original.id,
        CreditCancellationCreate(expected_version=1, reason="Fictional explicit credit end"),
        "fictional-explicit-credit-cancel",
    )
    ended = send(s, doc, ending, "fictional-end-with-credit")
    assert ended.replacement is None
    before = snapshot(s)
    assert credit(s, request) == original
    assert send(s, doc, ending, "fictional-end-with-credit") == ended
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_new_cancellation_can_reverse_disabled_old_asset_and_archived_references(drafts, kind):
    from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
    from coinpup_api.ledger.schemas import CategoryUpdate

    s = drafts
    doc, _ = confirmed(s, kind, 1)
    to_correct, _ = confirmed(s, kind, 1, key="fictional-confirm-to-reprice")
    s["structure"].update_asset(
        s["owner"], doc.asset_id, AssetUpdate(expected_version=1, enabled=False)
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as invalid:
        send(s, to_correct, correction(to_correct), "fictional-disabled-new-posting")
    assert (invalid.value.status, invalid.value.code) == (409, "asset_disabled")
    assert snapshot(s) == before
    # Both documents reverse the retired unit. Only this one also posts a new active unit.
    revised = correction(to_correct, asset_id="EUR")
    accepted = send(s, to_correct, revised, "fictional-active-replacement")
    assert (accepted.previous.asset_id, accepted.replacement.asset_id) == (doc.asset_id, "EUR")
    s["master"].update_party(
        s["owner"], s["ledger"], doc.party_id, PartyUpdate(expected_version=1, archived=True)
    )
    line = doc.lines[0]
    s["master"].update_project(
        s["owner"], s["ledger"], line.project_id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"], s["ledger"], line.category_id, CategoryUpdate(expected_version=1, archived=True)
    )
    ending = ConfirmedDocumentCancellation(
        expected_version=doc.version, reason="Fictional reverse frozen references"
    )
    result = send(s, doc, ending, "fictional-disabled-cancel")
    assert result.replacement is None and result.previous.asset_id == doc.asset_id
    before = snapshot(s)
    assert send(s, to_correct, revised, "fictional-active-replacement") == accepted
    assert snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_closed_zero_line_date_prevents_whole_revision_even_without_an_old_posting(drafts, action):
    from tests.integration.business.test_confirmation_service import prepare, submit

    s = drafts
    first = s["payload"]().model_dump()["lines"][0] | dict(
        id=uuid4(),
        category_id=s["income"].id,
        unit_price="1",
        quantity="1",
        tax_rate_percent="0",
        discount_amount="0",
        recognition_date="2026-10-20",
    )
    zero = first | dict(id=uuid4(), unit_price="0", recognition_date="2026-09-01")
    saved, intent = prepare(s, changes={"lines": [first, zero]})
    original = submit(s, saved, intent)
    assert len(original.postings) == 1
    with Session(s["engine"]) as db:
        doc = read_confirmed_document_version(db, s["ledger"], saved.id).document
    change_period(s, cutoff="2026-09-30")
    payload = (
        correction(doc)
        if action == "correct"
        else ConfirmedDocumentCancellation(
            expected_version=doc.version, reason="Fictional zero-date cancellation"
        )
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as blocked:
        send(s, doc, payload, "fictional-closed-zero")
    assert blocked.value.code == "period_closed"
    assert snapshot(s) == before
