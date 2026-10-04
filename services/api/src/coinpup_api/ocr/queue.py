"""Short fenced queue transactions. File access and recognition belong outside this service."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page, _version
from coinpup_api.ocr.contracts import (
    CONFIG_BYTES,
    Completion,
    JobCreate,
    JobResult,
    JobView,
    Lease,
    canonical_json,
    fence_hash,
    manifest_hash,
    prepare_completion,
    prepare_configuration,
)
from coinpup_api.ocr.models import OcrDraft, OcrJob

FAILURE_CODES = frozenset(
    {
        "source_unavailable",
        "invalid_document",
        "processor_timeout",
        "processor_unavailable",
        "processing_failed",
        "resource_limit",
        "configuration_invalid",
        "source_archived",
        "attempts_exhausted",
    }
)


def _error(code, message, status=409):
    return LedgerError(code, status, message)


def _lost():
    return _error("ocr_lease_lost", "The OCR lease is no longer valid.")


def _eligible():
    return or_(
        and_(OcrJob.state == "pending", OcrJob.retry_at <= func.clock_timestamp()),
        and_(OcrJob.state == "running", OcrJob.lease_until <= func.clock_timestamp()),
    )


class OcrQueueService(LedgerService):
    @staticmethod
    def _view(job) -> JobView:
        result = None
        if job.state == "succeeded":
            try:
                stored = job.result
                if (
                    type(stored["format"]) is not int
                    or stored["format"] != 1
                    or type(stored["summary"]) is not dict
                    or type(stored["draft_ids"]) is not list
                ):
                    raise ValueError
                summary = {
                    name: stored["summary"][name]
                    for name in ("pages", "manual_pages")
                    if type(stored["summary"].get(name)) is int
                    and 0 <= stored["summary"][name] <= 1000000
                }
                summary["candidates"] = len(stored["draft_ids"])
                result = JobResult(summary=summary, draft_ids=stored["draft_ids"])
            except (KeyError, TypeError, ValueError):
                raise _error(
                    "ocr_result_unavailable", "The OCR result is unavailable.", 503
                ) from None
        return JobView(
            **{name: getattr(job, name) for name in JobView.model_fields if name != "result"},
            result=result,
        )

    @staticmethod
    def _scope(session, owner_id, ledger_id):
        ledger = session.scalar(
            select(Ledger)
            .where(
                Ledger.id == ledger_id,
                Ledger.owner_id == owner_id,
            )
            .with_for_update()
        )
        if ledger is None:
            raise _not_found()
        entity = session.scalar(
            select(Entity)
            .where(
                Entity.id == ledger.entity_id,
                Entity.owner_id == owner_id,
            )
            .with_for_update()
        )
        if entity is None:
            raise _not_found()
        return entity

    @staticmethod
    def _job(session, owner_id, ledger_id, job_id, *, lock=False, skip=False):
        query = select(OcrJob).where(
            OcrJob.id == job_id,
            OcrJob.ledger_id == ledger_id,
            OcrJob.created_by == owner_id,
        )
        if lock:
            query = query.with_for_update(skip_locked=skip)
        job = session.scalar(query)
        if job is None and not skip:
            raise _not_found()
        return job

    @staticmethod
    def _source(session, owner_id, ledger_id, file_id):
        source = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.ledger_id == ledger_id,
                StoredFile.created_by == owner_id,
            )
        )
        if source is None:
            raise _not_found()
        return source

    @staticmethod
    def _active(entity, source):
        if entity.archived:
            raise _error("entity_archived", "Restore the entity before creating OCR work.")
        if source.archived:
            raise _error("file_archived", "Restore the file before creating OCR work.")

    @staticmethod
    def _lease(job, source):
        return Lease(
            owner_id=job.created_by,
            ledger_id=job.ledger_id,
            job_id=job.id,
            file_id=job.file_id,
            generation=job.generation,
            token=job.lease_token,
            lease_until=job.lease_until,
            configuration_json=canonical_json(job.configuration, CONFIG_BYTES),
            blob_key=source.blob_key,
            sha256=source.sha256,
            byte_size=source.byte_size,
            media_type=source.detected_media_type,
        )

    @staticmethod
    def _fence(lease):
        return and_(
            OcrJob.state == "running",
            OcrJob.lease_token == lease.token,
            OcrJob.generation == lease.generation,
            OcrJob.file_id == lease.file_id,
            OcrJob.lease_until > func.clock_timestamp(),
        )

    @staticmethod
    def _write(session, job, *, condition=None, **values):
        query = update(OcrJob).where(
            OcrJob.id == job.id,
            OcrJob.ledger_id == job.ledger_id,
            OcrJob.created_by == job.created_by,
            OcrJob.version == job.version,
        )
        if condition is not None:
            query = query.where(condition)
        record = session.scalar(
            query.values(
                **values,
                version=OcrJob.version + 1,
                updated_at=func.clock_timestamp(),
            )
            .returning(OcrJob)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        if record is None:
            raise _lost()
        return record

    @staticmethod
    def _source_matches(lease, job, source):
        if (
            job.file_id != lease.file_id
            or canonical_json(job.configuration, CONFIG_BYTES) != lease.configuration_json
            or (source.blob_key, source.sha256, source.byte_size, source.detected_media_type)
            != (lease.blob_key, lease.sha256, lease.byte_size, lease.media_type)
        ):
            raise _lost()

    def _leased(self, session, lease):
        entity = self._scope(session, lease.owner_id, lease.ledger_id)
        job = self._job(session, lease.owner_id, lease.ledger_id, lease.job_id, lock=True)
        source = self._source(session, lease.owner_id, lease.ledger_id, job.file_id)
        self._source_matches(lease, job, source)
        return entity, job, source

    def _terminate(self, session, job, code, *, condition=None):
        return self._view(
            self._write(
                session,
                job,
                condition=condition,
                state="failed",
                error_code=code,
                lease_token=None,
                lease_until=None,
                result=None,
            )
        )

    def create_job(self, owner_id, ledger_id, request: JobCreate, *, configuration) -> JobView:
        manifest = manifest_hash(owner_id, ledger_id, request)
        with self._transaction(owner_id, write=True) as session:
            entity = self._scope(session, owner_id, ledger_id)
            existing = session.scalar(
                select(OcrJob).where(
                    OcrJob.created_by == owner_id,
                    OcrJob.intent_id == request.intent_id,
                )
            )
            if existing is not None:
                if existing.ledger_id != ledger_id:
                    raise _not_found()
                if existing.manifest_hash != manifest:
                    raise _error("ocr_manifest_conflict", "The OCR request changed.")
                return self._view(existing)
            source = self._source(session, owner_id, ledger_id, request.file_id)
            self._active(entity, source)
            frozen, config_hash = prepare_configuration(configuration)
            job = OcrJob(
                ledger_id=ledger_id,
                created_by=owner_id,
                file_id=request.file_id,
                intent_id=request.intent_id,
                manifest_hash=manifest,
                configuration=frozen,
                config_hash=config_hash,
            )
            session.add(job)
            session.flush()
            return self._view(job)

    def get_job(self, owner_id, ledger_id, job_id) -> JobView:
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            return self._view(self._job(session, owner_id, ledger_id, job_id))

    def list_jobs(self, owner_id, ledger_id, *, intent_id=None, state=None, limit=100, offset=0):
        _page(limit, offset)
        if state is not None and state not in ("pending", "running", "succeeded", "failed"):
            raise _error("ocr_invalid_state", "The OCR state filter is invalid.", 422)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            query = select(OcrJob).where(
                OcrJob.ledger_id == ledger_id, OcrJob.created_by == owner_id
            )
            if intent_id is not None:
                query = query.where(OcrJob.intent_id == intent_id)
            if state is not None:
                query = query.where(OcrJob.state == state)
            return [
                self._view(job)
                for job in session.scalars(
                    query.order_by(
                        OcrJob.created_at.desc(),
                        OcrJob.id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ]

    def retry_job(self, owner_id, ledger_id, job_id, expected_version: int) -> JobView:
        if type(expected_version) is not int or expected_version < 1:
            raise _error("ocr_invalid_version", "The OCR version is invalid.", 422)
        with self._transaction(owner_id, write=True) as session:
            entity = self._scope(session, owner_id, ledger_id)
            job = self._job(session, owner_id, ledger_id, job_id, lock=True)
            _version(job, expected_version)
            if job.state != "failed":
                raise _error("ocr_not_retryable", "Only failed OCR work can be retried.")
            if job.attempts >= 3:
                raise _error("ocr_retry_exhausted", "The OCR attempt limit was reached.")
            self._active(entity, self._source(session, owner_id, ledger_id, job.file_id))
            return self._view(
                self._write(
                    session,
                    job,
                    state="pending",
                    retry_at=func.clock_timestamp(),
                    error_code=None,
                )
            )

    def claim(self, owner_id) -> Lease | None:
        skipped = []
        for _ in range(100):
            # A different candidate gets a new transaction: never lock ledger2 after entity1.
            with self._transaction(owner_id, write=True) as session:
                candidate = session.execute(
                    select(OcrJob.id, OcrJob.ledger_id)
                    .where(
                        OcrJob.created_by == owner_id,
                        _eligible(),
                        OcrJob.id.not_in(skipped),
                    )
                    .order_by(OcrJob.retry_at, OcrJob.created_at, OcrJob.id)
                    .limit(1)
                ).first()
                if candidate is None:
                    return None
                entity = self._scope(session, owner_id, candidate.ledger_id)
                job = self._job(
                    session, owner_id, candidate.ledger_id, candidate.id, lock=True, skip=True
                )
                if job is None:
                    skipped.append(candidate.id)
                    continue
                source = self._source(session, owner_id, job.ledger_id, job.file_id)
                if entity.archived or source.archived:
                    self._terminate(session, job, "source_archived", condition=_eligible())
                    return None
                if job.attempts >= 3:
                    self._terminate(session, job, "attempts_exhausted", condition=_eligible())
                    return None
                try:
                    config, digest = prepare_configuration(job.configuration)
                    if digest != job.config_hash:
                        raise ValueError
                except (LedgerError, ValueError):
                    self._terminate(session, job, "configuration_invalid", condition=_eligible())
                    return None
                claimed = self._write(
                    session,
                    job,
                    condition=_eligible(),
                    state="running",
                    attempts=job.attempts + 1,
                    generation=job.generation + 1,
                    lease_token=uuid4(),
                    lease_until=func.clock_timestamp() + timedelta(seconds=config["lease_seconds"]),
                    error_code=None,
                    result=None,
                )
                return self._lease(claimed, source)
        return None

    def renew(self, lease: Lease) -> Lease | JobView:
        with self._transaction(lease.owner_id, write=True) as session:
            entity, job, source = self._leased(session, lease)
            if entity.archived or source.archived:
                return self._terminate(
                    session, job, "source_archived", condition=self._fence(lease)
                )
            renewed = self._write(
                session,
                job,
                condition=self._fence(lease),
                lease_until=func.greatest(
                    job.lease_until,
                    func.clock_timestamp() + timedelta(seconds=job.configuration["lease_seconds"]),
                ),
            )
            return self._lease(renewed, source)

    def finish(self, lease: Lease, completion: Completion) -> JobView:
        data, payload_hash = prepare_completion(completion)
        fence = fence_hash(lease)
        with self._transaction(lease.owner_id, write=True) as session:
            entity, job, source = self._leased(session, lease)
            if job.state == "succeeded":
                if job.result.get("fence_hash") != fence:
                    raise _lost()
                if job.result.get("payload_hash") != payload_hash:
                    raise _error("ocr_completion_conflict", "The OCR completion changed.")
                return self._view(job)
            # Reject stale workers before touching candidates; the final UPDATE repeats this CAS.
            if (
                session.scalar(
                    select(OcrJob.id).where(
                        OcrJob.id == job.id,
                        self._fence(lease),
                    )
                )
                is None
            ):
                raise _lost()
            if entity.archived or source.archived:
                return self._terminate(
                    session, job, "source_archived", condition=self._fence(lease)
                )
            draft_ids = []
            for candidate in data["candidates"]:
                session.execute(
                    insert(OcrDraft)
                    .values(
                        ledger_id=job.ledger_id,
                        created_by=job.created_by,
                        job_id=job.id,
                        **candidate,
                    )
                    .on_conflict_do_nothing(constraint="uq_ocr_drafts_job_source")
                )
                draft_ids.append(
                    str(
                        session.scalar(
                            select(OcrDraft.id).where(
                                OcrDraft.job_id == job.id,
                                OcrDraft.source_key == candidate["source_key"],
                            )
                        )
                    )
                )
            result = {
                "format": 1,
                "fence_hash": fence,
                "payload_hash": payload_hash,
                "summary": data["summary"],
                "draft_ids": draft_ids,
            }
            return self._view(
                self._write(
                    session,
                    job,
                    condition=self._fence(lease),
                    state="succeeded",
                    result=result,
                    error_code=None,
                    lease_token=None,
                    lease_until=None,
                )
            )

    def fail(self, lease: Lease, error_code: str, *, retryable=False) -> JobView:
        if (
            type(error_code) is not str
            or error_code not in FAILURE_CODES
            or type(retryable) is not bool
        ):
            raise _error("ocr_invalid_payload", "The OCR failure code is invalid.", 422)
        with self._transaction(lease.owner_id, write=True) as session:
            entity, job, source = self._leased(session, lease)
            if entity.archived or source.archived:
                return self._terminate(
                    session, job, "source_archived", condition=self._fence(lease)
                )
            pending = retryable and job.attempts < 3
            return self._view(
                self._write(
                    session,
                    job,
                    condition=self._fence(lease),
                    state="pending" if pending else "failed",
                    error_code=error_code,
                    lease_token=None,
                    lease_until=None,
                    result=None,
                    retry_at=func.clock_timestamp()
                    + timedelta(seconds=job.configuration["retry_seconds"]),
                )
            )
