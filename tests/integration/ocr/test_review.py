"""Fictional draft edits keep original evidence, row versions and finance boundaries."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.models import OcrDraft
from coinpup_api.ocr.review import DraftReviewService, DraftReviewUpdate, HumanReview
from sqlalchemy import func, update

from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ocr.test_bound_transactions import bound_setup as bound_setup
from tests.integration.ocr.test_confirmation_schema import confirmation_setup as confirmation_setup
from tests.integration.ocr.test_confirmation_schema import write
from tests.integration.ocr.test_draft_reads import drafts as drafts
from tests.integration.ocr.test_ocr_queue import archive, finances
from tests.integration.ocr.test_ocr_queue import queue_structure as queue_structure
from tests.integration.ocr.test_ocr_schema import ocr_structure as ocr_structure
from tests.integration.ocr.test_ocr_schema import rows, writer

pytestmark = pytest.mark.integration


def test_confirmed_review_is_readable_but_never_editable(confirmation_setup):
    s = confirmation_setup
    write(s)
    service = DraftReviewService(s["engine"])
    draft_id = s["confirmation"]["draft_id"]
    assert service.get_review(s["owner"], s["ledger"], draft_id).status == "confirmed"
    before = rows(s["engine"])
    with pytest.raises(LedgerError) as error:
        service.update_review(
            s["owner"],
            s["ledger"],
            draft_id,
            DraftReviewUpdate(
                expected_version=2,
                review=HumanReview(),
            ),
        )
    assert error.value.code == "ocr_already_confirmed" and rows(s["engine"]) == before


def setup(s):
    return DraftReviewService(s["engine"]), s["receipt"].result.draft_ids[0], s["ledgers"][0]


def test_review_save_changes_only_human_fields_and_one_version_event(drafts):
    s = drafts
    service, identifier, ledger = setup(s)
    before = s["reader"].get_draft(s["owner"], ledger, identifier)
    finance_before = finances(s["engine"])
    fields = service.get_review(s["owner"], ledger, identifier).fields
    payload = DraftReviewUpdate(
        expected_version=1,
        review=HumanReview(
            confirmed=[fields[0].path],
            entry={"amount": "12.30", "fictional_note": "incomplete"},
        ),
    )
    saved = service.update_review(s["owner"], ledger, identifier, payload)
    assert saved.version == 2 and saved.review == payload.review
    after = s["reader"].get_draft(s["owner"], ledger, identifier)
    assert (after.recognized, after.evidence, after.recognition) == (
        before.recognized,
        before.evidence,
        before.recognition,
    )
    assert finances(s["engine"]) == finance_before
    events = rows(s["engine"])["change_log"]
    assert events[-1]["entity_type"] == "ocr_drafts" and events[-1]["entity_version"] == 2
    committed = rows(s["engine"])
    with pytest.raises(LedgerError) as error:
        service.update_review(s["owner"], ledger, identifier, payload)
    assert error.value.code == "version_conflict" and rows(s["engine"]) == committed


def test_review_ignoring_and_restoring_never_posts_or_auto_confirms(drafts):
    s = drafts
    service, identifier, ledger = setup(s)
    before = finances(s["engine"])
    for version, status in [(1, "ignored"), (2, "draft")]:
        result = service.update_review(
            s["owner"],
            ledger,
            identifier,
            DraftReviewUpdate(
                expected_version=version,
                status=status,
                review=HumanReview(),
            ),
        )
        assert result.status == status and result.review.confirmed == []
        assert all(field.requires_confirmation for field in result.fields)
    assert finances(s["engine"]) == before


def test_review_recomputes_policy_even_if_existing_editable_metadata_was_forged(drafts):
    s = drafts
    service, identifier, ledger = setup(s)
    with writer(s) as session:
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == identifier)
            .values(
                version=2,
                updated_at=func.clock_timestamp(),
                fields={"review": []},
            )
        )
    assert all(
        field.requires_confirmation
        for field in service.get_review(s["owner"], ledger, identifier).fields
    )
    before = rows(s["engine"])
    with pytest.raises(LedgerError) as error:
        service.update_review(
            s["owner"],
            ledger,
            identifier,
            DraftReviewUpdate(
                expected_version=2,
                review=HumanReview(confirmed=["fictional-forged"]),
            ),
        )
    assert error.value.code == "ocr_invalid_review" and rows(s["engine"]) == before


def test_concurrent_review_writers_cannot_overwrite_each_other(drafts):
    s = drafts
    service, identifier, ledger = setup(s)

    def edit(value):
        try:
            return service.update_review(
                s["owner"],
                ledger,
                identifier,
                DraftReviewUpdate(
                    expected_version=1,
                    review=HumanReview(entry={"fictional_choice": value}),
                ),
            ).review.entry["fictional_choice"]
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(edit, ["left", "right"]))
    assert result.count("version_conflict") == 1
    saved = service.get_review(s["owner"], ledger, identifier)
    assert saved.version == 2 and saved.review.entry["fictional_choice"] in result


def test_review_respects_owner_ledger_and_archived_entity(drafts):
    s = drafts
    service, identifier, ledger = setup(s)
    payload = DraftReviewUpdate(expected_version=1, review=HumanReview())
    for owner, target in [(uuid4(), ledger), (s["owner"], s["ledgers"][1])]:
        with pytest.raises(LedgerError) as error:
            service.update_review(owner, target, identifier, payload)
        assert error.value.code == "not_found"
    archive(s, "entity")
    assert service.get_review(s["owner"], ledger, identifier).version == 1
    with pytest.raises(LedgerError) as error:
        service.update_review(s["owner"], ledger, identifier, payload)
    assert error.value.code == "entity_archived"
