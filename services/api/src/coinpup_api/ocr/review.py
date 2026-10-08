"""Versioned human work in progress; immutable evidence remains the review authority."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictStr, TypeAdapter, ValidationError, field_validator
from sqlalchemy import select

from coinpup_api.ledger.schemas import Version
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _touch, _version

from .contracts import CANDIDATE_BYTES, InputModel, canonical_json
from .models import OcrDraft, OcrJob
from .runtime import field_review

ReviewPath = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class HumanReview(InputModel):
    confirmed: Annotated[list[ReviewPath], Field(max_length=1000)] = Field(default_factory=list)
    entry: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("confirmed")
    @classmethod
    def distinct_paths(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Each field may be reviewed only once")
        return value


class DraftReviewUpdate(InputModel):
    expected_version: Version
    status: Literal["draft", "ignored"] = "draft"
    review: HumanReview


class ReviewField(BaseModel):
    path: str
    source: Literal["text", "ocr", "unknown"]
    candidate_value: str | None
    suggested_value: str | None
    requires_confirmation: bool


class ReviewView(BaseModel):
    draft_id: UUID
    version: int
    status: Literal["draft", "ignored", "confirmed"]
    fields: list[ReviewField]
    review: HumanReview


def original_review(draft, job):
    """Never let edited fields or a text page authorize an OCR-derived candidate."""
    try:
        selected = draft.recognized.get("fields", [])
        if not selected:
            return []  # Legacy/manual drafts still require a valid explicit financial command.
        paths = [item["path"] for item in selected]
        if len(paths) != len(set(paths)) or len(paths) > 1000:
            raise ValueError
        recognition = job.result["summary"]["recognition"]
        by_path = {item["path"]: item for item in field_review(recognition)}
        return TypeAdapter(list[ReviewField]).validate_python([by_path[path] for path in paths])
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise LedgerError(
            "ocr_invalid_evidence", 503, "Original review evidence is invalid."
        ) from None


def reviewed_fields(draft, job, review, *, complete=False):
    canonical_json(review.entry, CANDIDATE_BYTES)
    fields = original_review(draft, job)
    allowed = {item.path for item in fields}
    confirmed = set(review.confirmed)
    required = {item.path for item in fields if item.requires_confirmation}
    if not confirmed <= allowed:
        raise LedgerError("ocr_invalid_review", 422, "Review contains an unknown field.")
    if complete and not required <= confirmed:
        raise LedgerError(
            "ocr_review_required", 422, "Confirm every required field before posting."
        )
    result = {
        "version": 1,
        "review": [item.model_dump(mode="json") for item in fields],
        **review.model_dump(mode="json"),
    }
    canonical_json(result, CANDIDATE_BYTES)
    return result


def scoped_draft(session, owner_id, ledger_id, draft_id, *, write=False):
    query = select(OcrDraft).where(
        OcrDraft.id == draft_id, OcrDraft.created_by == owner_id, OcrDraft.ledger_id == ledger_id
    )
    if write:
        query = query.with_for_update()
    draft = session.scalar(query)
    if draft is None:
        raise _not_found()
    job = session.get(OcrJob, draft.job_id)
    if job is None or job.created_by != owner_id or job.ledger_id != ledger_id:
        raise _not_found()
    return draft, job


def review_view(draft, job):
    try:
        human = HumanReview.model_validate(
            {"confirmed": draft.fields.get("confirmed", []), "entry": draft.fields.get("entry", {})}
        )
        reviewed_fields(draft, job, human)
    except ValidationError:
        raise LedgerError("ocr_invalid_review", 503, "Stored review is invalid.") from None
    return ReviewView(
        draft_id=draft.id,
        version=draft.version,
        status=draft.status,
        fields=original_review(draft, job),
        review=human,
    )


class DraftReviewService(LedgerService):
    def get_review(self, owner_id, ledger_id, draft_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return review_view(*scoped_draft(session, owner_id, ledger_id, draft_id))

    def update_review(self, owner_id, ledger_id, draft_id, payload):
        canonical_json(payload.model_dump(mode="python"), CANDIDATE_BYTES)
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            draft, job = scoped_draft(session, owner_id, ledger_id, draft_id, write=True)
            _version(draft, payload.expected_version)
            if draft.status == "confirmed":
                raise LedgerError(
                    "ocr_already_confirmed", 409, "A confirmed draft cannot be edited."
                )
            if job.state != "succeeded":
                raise LedgerError("ocr_job_incomplete", 409, "Recognition has not completed.")
            draft.fields = reviewed_fields(draft, job, payload.review)
            draft.status = payload.status
            _touch(draft)
            session.flush()
            return review_view(draft, job)
