"""Fictional bank evidence adds no money and seals the complete original payment receipt."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.files.models import StoredFile
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
from coinpup_api.ocr.models import OcrConfirmation, OcrDraft
from coinpup_api.ocr.payment_confirmation_contracts import PaymentLinkConfirmationResponse
from coinpup_api.ocr.queue import OcrQueueService
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay, prepared
from tests.integration.conftest import payment_confirmation_migration
from tests.integration.files.test_files_service import complete, reserve
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ocr.test_bound_transactions import bound_setup as bound_setup
from tests.integration.ocr.test_confirmation_schema import (
    confirmation_setup as confirmation_setup,
)
from tests.integration.ocr.test_confirmation_schema import write as write_legacy
from tests.integration.ocr.test_confirmation_schema import writer

pytestmark = pytest.mark.integration
ALLOWED = {"ocr_confirmations", "ocr_drafts", "operation_file_links", "change_log"}


def setup(s, tmp_path, *, count=1, initial=True, fees=False):
    payload, _ = prepared(s, count=count, initial=initial)
    if not fees:
        payload = payload.model_copy(update={"fees": []})
    payment = pay(s, payload)
    files = s | dict(
        service=DocumentService(s["engine"]), store=FileStore(tmp_path / "private", 1024, 5)
    )
    upload, _ = reserve(files)
    file = complete(files, upload).file_id
    queue = OcrQueueService(s["engine"])
    queue.create_job(
        s["owner"],
        s["ledger"],
        JobCreate(intent_id=uuid4(), file_id=file),
        configuration={"lease_seconds": 120, "retry_seconds": 0, "processing": {}},
    )
    result = queue.finish(
        queue.claim(s["owner"]),
        Completion(
            candidates=[
                Candidate(
                    source_key="document:0",
                    recognized={"text": "Fictional bank payment 100.00"},
                    evidence={"pages": [1]},
                    fields={},
                )
            ]
        ),
    )
    draft, intent = result.result.draft_ids[0], uuid4()
    response = PaymentLinkConfirmationResponse(
        intent_id=intent,
        draft_id=draft,
        ledger_id=s["ledger"],
        draft_version=2,
        action="link_payment",
        target_ledger_id=s["ledger"],
        target_file_id=file,
        payment=payment,
        operation_version=1,
    )
    row = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        created_by=s["owner"],
        draft_id=draft,
        intent_id=intent,
        source_file_id=file,
        source_key="document:0",
        target_ledger_id=s["ledger"],
        target_file_id=file,
        operation_id=payment.operation_id,
        payment_id=payment.id,
        draft_version=1,
        operation_version=1,
        action="link_payment",
        request_hash="a" * 64,
        review={},
        response=response.model_dump(mode="json"),
    )
    return s | dict(
        files=files["service"], confirmation=row, receipt=response, payment_body=payload
    )


def write(s, **changes):
    row = s["confirmation"] | changes
    with writer(s) as session:
        file = session.get(StoredFile, row["target_file_id"])
        s["files"]._link(session, s["owner"], s["ledger"], row["operation_id"], file)
        session.execute(insert(OcrConfirmation).values(row))
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == row["draft_id"])
            .values(
                fields=row["review"],
                status="confirmed",
                version=2,
                updated_at=func.clock_timestamp(),
            )
        )


def migrate(s, action):
    with s["engine"].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            payment_confirmation_migration()[action]()


@pytest.mark.parametrize(
    "count,initial,fees", [(1, False, True), (1, True, False), (200, True, False)]
)
def test_link_seals_full_original_receipt_and_changes_no_financial_table(
    drafts, tmp_path, count, initial, fees
):
    s = setup(drafts, tmp_path, count=count, initial=initial, fees=fees)
    before = snapshot(s)
    write(s)
    after = snapshot(s)
    assert {k: v for k, v in after.items() if k not in ALLOWED} == {
        k: v for k, v in before.items() if k not in ALLOWED
    }
    assert after["ocr_confirmations"][0]["response"] == s["receipt"].model_dump(mode="json")
    assert after["ocr_confirmations"][0]["payment_id"] == s["receipt"].payment.id
    assert len(after["operation_file_links"]) == len(before["operation_file_links"]) + 1
    assert [row["entity_type"] for row in after["change_log"][-3:]] == [
        "operation_file_links",
        "ocr_confirmations",
        "ocr_drafts",
    ]
    if initial:
        assert (
            len(
                after["ocr_confirmations"][0]["response"]["payment"]["initial_allocation"][
                    "postings"
                ]
            )
            == count
        )


@pytest.mark.parametrize(
    "damage",
    [
        "payment",
        "other_payment",
        "missing",
        "legacy",
        "operation",
        "version",
        "intent",
        "partial",
        "unordered",
        "journal",
        "extra",
        "numeric_version",
    ],
)
def test_direct_sql_bad_identity_or_partial_200_member_receipt_rolls_back_all(
    drafts, tmp_path, damage
):
    s = setup(drafts, tmp_path, count=200 if damage in {"partial", "unordered"} else 1)
    changes = {}
    row = s["confirmation"]
    if damage == "payment":
        changes["payment_id"] = uuid4()
    elif damage == "other_payment":
        another = s["payment_body"].model_copy(update={"id": uuid4(), "initial_allocation": None})
        changes["payment_id"] = pay(s, another, "fictional-other-payment").id
    elif damage == "missing":
        changes["payment_id"] = None
    elif damage == "legacy":
        changes["action"] = "link"
    elif damage == "operation":
        changes["operation_id"] = row["response"]["payment"]["initial_allocation"]["postings"][0][
            "operation_id"
        ]
    elif damage == "version":
        changes["operation_version"] = 2
    else:
        response = row["response"]
        if damage == "intent":
            response["intent_id"] = str(uuid4())
        elif damage == "partial":
            response["payment"]["initial_allocation"]["postings"].pop()
        elif damage == "unordered":
            response["payment"]["initial_allocation"]["postings"].reverse()
        elif damage == "journal":
            response["payment"]["journal_id"] = str(uuid4())
        elif damage == "numeric_version":
            response["operation_version"] = 1.0
        else:
            response["operation"] = {}
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        write(s, **changes)
    assert snapshot(s) == before


def test_confirmed_payment_evidence_is_immutable_and_blocks_downgrade(drafts, tmp_path):
    s = setup(drafts, tmp_path)
    write(s)
    before = snapshot(s)
    for statement in [
        delete(OcrConfirmation),
        update(OcrConfirmation).values(payment_id=None, version=2),
    ]:
        with pytest.raises(IntegrityError) as error:
            with writer(s) as session:
                session.execute(statement)
        assert error.value.orig.diag.constraint_name == "ck_ocr_confirmations_immutable"
        assert snapshot(s) == before
    with pytest.raises(IntegrityError) as error:
        migrate(s, "downgrade")
    assert error.value.orig.diag.constraint_name == "ck_ocr_payment_confirmation_downgrade"
    assert snapshot(s) == before


def test_linking_evidence_in_closed_financial_period_does_not_post_money(drafts, tmp_path):
    s = setup(drafts, tmp_path)
    change_period(s, cutoff="2026-09-30")
    before = snapshot(s)
    write(s)
    after = snapshot(s)
    assert {k: v for k, v in after.items() if k not in ALLOWED} == {
        k: v for k, v in before.items() if k not in ALLOWED
    }


def test_existing_legacy_receipt_json_and_digest_survive_upgrade_unchanged(confirmation_setup):
    s = confirmation_setup
    migrate(s, "downgrade")
    # Use Core INSERT to the frozen table: the current ORM has a deliberately absent new column.
    with writer(s) as session:
        session.execute(insert(OcrConfirmation.__table__).values(s["confirmation"]))
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == s["confirmation"]["draft_id"])
            .values(
                fields=s["confirmation"]["review"],
                status="confirmed",
                version=2,
                updated_at=func.clock_timestamp(),
            )
        )
    with s["engine"].connect() as c:
        before = c.execute(
            select(OcrConfirmation.response, OcrConfirmation.request_hash, OcrConfirmation.review)
        ).one()
    migrate(s, "upgrade")
    with s["engine"].connect() as c:
        after = c.execute(
            select(
                OcrConfirmation.response,
                OcrConfirmation.request_hash,
                OcrConfirmation.review,
                OcrConfirmation.payment_id,
            )
        ).one()
    assert tuple(after[:3]) == tuple(before) and after.payment_id is None


def test_legacy_only_round_trip_retains_data_and_restores_old_action_constraint(confirmation_setup):
    s = confirmation_setup
    write_legacy(s)
    before = snapshot(s)
    migrate(s, "downgrade")
    with s["engine"].connect() as c:
        constraint = c.exec_driver_sql(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_ocr_confirmations_action'"
        ).scalar_one()
        assert "link_payment" not in constraint
    migrate(s, "upgrade")
    assert snapshot(s) == before
