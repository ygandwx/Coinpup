"""Generate complete whole-document 200s from immutable SQL facts and replay original raw intent."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.confirmation_hash import document_revision_hash_v2
from coinpup_api.business.document_fixed_receipts import (
    document_receipt_anchor,
    fixed_document_response,
    replay_document_revision,
)
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_links import accrual, post
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def save(c, db, header, action, command):
    body = command.model_dump(mode="json", exclude_unset=True)
    raw = json.dumps(body).encode()
    ledger, document = header["ledger_id"], header["document_id"]
    digest = document_revision_hash_v2(action, ledger, document, command, raw_body=raw)
    fixed = fixed_document_response(db, ledger, document, header["id"])
    c.execute(
        insert(CommandReceipt),
        dict(
            ledger_id=ledger,
            key=header["idempotency_key"],
            hash_version=2,
            request_hash=digest,
            response_status=200,
            response=fixed.model_dump(mode="json"),
            operation_id=document_receipt_anchor(fixed),
        ),
    )
    return fixed, db.get(CommandReceipt, (ledger, header["idempotency_key"])), digest


def test_document_receipts_stay_fixed_after_terminal_and_keep_raw_price_spelling(revision):
    s, header, doc, _, origin = revision
    header = header | dict(idempotency_key="fictional-correct-fixed-document", notes=None)
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        c.execute(insert(Header), header)
        fact = line(header, doc) | dict(
            quantity="1.500",
            unit_price="0.01",
            discount_amount="-0.00",
            tax_rate_percent="25.00",
            net_amount="0.02",
            tax_amount="0.01",
            total_amount="0.03",
        )
        zero = fact | dict(
            id=uuid4(),
            source_line_id=uuid4(),
            line_no=2,
            unit_price="-0.00",
            net_amount="0",
            tax_amount="0",
            total_amount="0",
        )
        c.execute(insert(Fact), [fact, zero])
        row = accrual(header, fact, origin.postings[0], "correct")
        post(c, header, fact, row)
        c.execute(insert(Link), row)
        proposed = replacement(
            doc,
            {
                doc.lines[0].id: {
                    field: fact[field]
                    for field in ("quantity", "unit_price", "discount_amount", "tax_rate_percent")
                }
            },
            notes=None,
        )
        proposed = proposed.model_copy(
            update={
                "lines": [
                    proposed.lines[0],
                    proposed.lines[0].model_copy(
                        update={"id": zero["source_line_id"], "unit_price": "-0.00"}
                    ),
                ]
            }
        )
        correction = ConfirmedDocumentCorrection(
            expected_version=origin.document_version, reason=header["reason"], replacement=proposed
        )
        first, first_receipt, digest = save(c, db, header, "correct", correction)
        assert first.replacement.total_amount == "0.03" and first.replacement.tax_amount == "0.01"
        assert [p.unit_price for p in first.replacement.lines] == ["0.01", "-0.00"]
        assert first.replacement.lines[1].posting is None
        terminal = cancelled(header) | dict(
            id=uuid4(),
            previous_revision_id=header["id"],
            idempotency_key="fictional-terminal-fixed-document",
            previous_version=first.version,
            document_version=first.version + 1,
        )
        c.execute(insert(Header), terminal)
        last = accrual(terminal, fact, origin.postings[0], "cancel") | dict(
            operation_version=3, previous_journal_id=row["journal_id"]
        )
        post(c, terminal, fact, last)
        c.execute(insert(Link), last)
        command = ConfirmedDocumentCancellation(
            expected_version=first.version, reason=terminal["reason"]
        )
        second, second_receipt, last_digest = save(c, db, terminal, "cancel", command)
        assert second.replacement is None and second.previous == first.replacement
        assert [r.journal_id for r in second.reversals] == [last["reversal_journal_id"]]
        assert (
            replay_document_revision(
                db, first_receipt, s["ledger"], doc.id, "correct", correction, digest
            )
            == first
        )
        assert (
            replay_document_revision(
                db, second_receipt, s["ledger"], doc.id, "cancel", command, last_digest
            )
            == second
        )
        assert db.get(
            CommandReceipt, (s["ledger"], "fictional-confirm")
        ).response == origin.model_dump(mode="json")
        altered = deepcopy(correction.model_dump(mode="json", exclude_unset=True))
        altered["replacement"]["lines"][0]["quantity"] = "1.5"
        other = document_revision_hash_v2(
            "correct", s["ledger"], doc.id, correction, raw_body=json.dumps(altered).encode()
        )
        with pytest.raises(LedgerError) as err:
            replay_document_revision(
                db, first_receipt, s["ledger"], doc.id, "correct", correction, other
            )
        assert err.value.code == "idempotency_conflict"
        assert (
            c.scalar(
                select(
                    func.coinpup_confirmed_document_revision_response(uuid4(), doc.id, header["id"])
                )
            )
            is None
        )
        assert (
            c.scalar(
                select(
                    func.coinpup_confirmed_document_revision_response(
                        s["ledger"], uuid4(), header["id"]
                    )
                )
            )
            is None
        )
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("missing", ["group", "old", "new"])
def test_incomplete_receipt_is_explicit_and_downgrade_protects_pending_header(revision, missing):
    s, h, doc, _, original = revision
    before = snapshot(s)
    with pytest.raises(LedgerError) as err, s["engine"].begin() as c, Session(c) as db:
        c.execute(insert(Header), h)
        if missing != "group":
            f = line(h, doc)
            if missing == "old":
                f["source_line_id"] = uuid4()
            c.execute(insert(Fact), f)
            if missing == "old":
                new = accrual(h, f, original.postings[0], "create")
                post(c, h, f, new)
                c.execute(insert(Link), new)
        fixed_document_response(db, s["ledger"], doc.id, h["id"])
    assert err.value.code == "document_revision_integrity" and snapshot(s) == before


def test_frozen_200_functions_survive_empty_roundtrip_and_refuse_header_history(revision):
    import runpy
    from pathlib import Path

    from tests.integration.business.test_credit_revision_storage import definitions

    s, h, *_ = revision
    m = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions"
            / "20261110_0079_confirmed_document_revision_receipts.py"
        )
    )
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = definitions(c)
        m["downgrade"]()
        m["upgrade"]()
        assert definitions(c) == catalog
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as err,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        c.execute(insert(Header), h)
        m["downgrade"]()
    assert (
        err.value.orig.diag.constraint_name == "ck_document_revision_receipt_downgrade"
        and snapshot(s) == before
    )


def test_two_hundred_predecessors_and_replacements_keep_all_four_hundred_journal_facts(revision):
    s, pattern, *_ = revision
    doc, old = confirmed(s, "bill", 200, key="fictional-large-fixed-document")
    h = pattern | dict(
        id=uuid4(),
        document_id=doc.id,
        confirmation_id=doc.confirmation_id,
        previous_version=doc.version,
        document_version=doc.version + 1,
        issue_date=doc.issue_date,
        idempotency_key="fictional-large-receipt",
    )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        c.execute(insert(Header), h)
        facts = [
            line(h, doc) | dict(id=uuid4(), line_no=i, source_line_id=uuid4())
            for i in range(1, 201)
        ]
        c.execute(insert(Fact), facts)
        changes = []
        for f, prior in zip(facts, old, strict=True):
            create = accrual(h, f, prior, "create")
            post(c, h, f, create, kind="bill")
            cancel = accrual(h, f | {"source_line_id": prior.source_line_id}, prior, "cancel")
            post(c, h, f, cancel)
            changes.extend([create, cancel])
        c.execute(insert(Link), changes)
        result = fixed_document_response(db, doc.ledger_id, doc.id, h["id"])
        assert (
            len(result.previous.lines)
            == len(result.replacement.lines)
            == len(result.reversals)
            == 200
        )
        assert [r.id for r in result.replacement.lines] == [f["source_line_id"] for f in facts]
        assert [r.source_line_id for r in result.reversals] == [p.source_line_id for p in old]
        proposed = replacement(doc, notes=h["notes"])
        proposed = proposed.model_copy(
            update={
                "lines": [
                    proposed.lines[0].model_copy(update={"id": f["source_line_id"]}) for f in facts
                ]
            }
        )
        command = ConfirmedDocumentCorrection(
            expected_version=doc.version, reason=h["reason"], replacement=proposed
        )
        saved, receipt, digest = save(c, db, h, "correct", command)
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_command_document_revision, "
            "zz_tr_document_revision_receipt IMMEDIATE"
        )
        assert (
            saved
            == result
            == replay_document_revision(
                db, receipt, doc.ledger_id, doc.id, "correct", command, digest
            )
        )
        assert (
            len(
                {
                    r.posting.operation_id
                    for g in (result.previous, result.replacement)
                    for r in g.lines
                }
            )
            == 400
        )
        tx.rollback()
    assert snapshot(s) == before
