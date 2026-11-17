"""Newly reviewed sources keep the same FK identity without entering the original confirmation."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_models import BusinessConfirmation
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.draft_schemas import BusinessDraftUpdate
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.models import BusinessDocumentLine as Line
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_new_source_keeps_original_snapshot_reads_and_201_exactly_intact(revision, monkeypatch):
    s, header, doc, intent, original = revision
    reader = BusinessConfirmationReads(s["engine"])
    args = (s["owner"], s["ledger"], doc.id)
    original_detail = reader.get_confirmation(*args)
    original_list = reader.list_confirmations(*args[:2])
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c, Session(c) as db:
        saved = c.scalar(
            select(BusinessConfirmation.snapshot).where(BusinessConfirmation.id == original.id)
        )
        c.execute(insert(Header), header)
        member = line(header, doc) | {"source_line_id": uuid4()}
        c.execute(insert(Fact), member)
        c.exec_driver_sql("SET CONSTRAINTS fk_document_revision_line_source IMMEDIATE")
        identity = db.get(Line, member["source_line_id"])
        assert (identity.document_id, identity.ledger_id, identity.version, identity.archived) == (
            doc.id,
            s["ledger"],
            1,
            False,
        )
        assert identity.quantity is identity.line_no is identity.total_amount is None
        assert c.scalar(select(func.coinpup_business_source_snapshot(doc.id))) == saved
        c.execute(select(func.coinpup_validate_business_confirmation(original.id)))
        # Exercise the real reads inside the pending writer, before the closed gate rolls it back.
        with monkeypatch.context() as m:
            m.setattr(reader, "_transaction", lambda *a, **kw: nullcontext(db))
            assert reader.get_confirmation(*args) == original_detail
            assert reader.list_confirmations(*args[:2]) == original_list
        c.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
    assert error.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert submit(s, doc, intent) == original and snapshot(s) == before


@pytest.mark.parametrize("origin", ["same-ledger", "other-ledger"])
def test_registered_identity_cannot_borrow_another_documents_line(revision, origin):
    s, header, doc, *_ = revision
    if origin == "same-ledger":
        other, _ = prepare(s)
        source_id = other.lines[0].id
    else:
        ledger = (
            s["structure"]
            .create_entity(
                s["owner"],
                EntityCreate(
                    kind="personal",
                    name="Fictional unrelated ledger",
                    base_asset_id="USD",
                    template_key="personal_default",
                ),
            )
            .ledger.id
        )
        document, source_id = uuid4(), uuid4()
        with s["engine"].begin() as c:
            c.execute(insert(BusinessDocument), dict(id=document, ledger_id=ledger))
            c.execute(insert(Line), dict(id=source_id, document_id=document, ledger_id=ledger))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        c.execute(insert(Fact), line(header, doc) | {"source_line_id": source_id})
    assert error.value.orig.diag.constraint_name == "ck_document_revision_line_source"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "attack,expected",
    [
        ("unregistered", "ck_business_line_draft"),
        ("edit-new", "ck_business_line_draft"),
        ("delete-new", "ck_business_reference_identity"),
        ("edit-original", "ck_business_line_draft"),
        ("reprice-original", "ck_business_line_draft"),
    ],
)
def test_sealed_document_line_guards_survive_source_reservation(revision, attack, expected):
    s, header, doc, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        source_id = uuid4()
        c.execute(insert(Fact), line(header, doc) | {"source_line_id": source_id})
        if attack == "unregistered":
            c.execute(insert(Line), dict(id=uuid4(), ledger_id=s["ledger"], document_id=doc.id))
        elif attack == "delete-new":
            c.execute(delete(Line).where(Line.id == source_id))
        elif attack == "reprice-original":
            c.execute(
                update(Line).where(Line.id == doc.lines[0].id).values(unit_price="9.99", version=2)
            )
        else:
            identifier = source_id if attack == "edit-new" else doc.lines[0].id
            c.execute(update(Line).where(Line.id == identifier).values(archived=True, version=2))
    assert error.value.orig.diag.constraint_name == expected and snapshot(s) == before


def test_later_version_reuses_revision_born_source_and_never_changes_its_metadata(revision):
    s, header, doc, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        member = line(header, doc) | {"source_line_id": uuid4()}
        c.execute(insert(Fact), member)
        created = c.execute(select(Line.__table__).where(Line.id == member["source_line_id"])).one()
        next_header = header | dict(
            id=uuid4(),
            previous_revision_id=header["id"],
            idempotency_key="fictional-second-source-version",
            previous_version=header["document_version"],
            document_version=header["document_version"] + 1,
        )
        c.execute(insert(Header), next_header)
        c.execute(
            insert(Fact),
            member
            | dict(
                id=uuid4(),
                revision_id=next_header["id"],
                document_version=next_header["document_version"],
                quantity="2",
                net_amount="200",
                tax_amount="20",
                total_amount="220",
            ),
        )
        assert (
            created
            == c.execute(select(Line.__table__).where(Line.id == member["source_line_id"])).one()
        )
        c.exec_driver_sql("SET CONSTRAINTS fk_document_revision_line_source IMMEDIATE")
        c.execute(text("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE"))
    assert error.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize("retired", [True, False])
def test_sparse_original_zero_and_removed_draft_source_remain_distinct(revision, retired):
    s, pattern, *_ = revision
    raw = s["payload"]()
    original, removed, zero = raw.lines[0], uuid4(), uuid4()
    raw = raw.model_copy(
        update={
            "lines": [
                original,
                original.model_copy(update={"id": removed}),
                original.model_copy(update={"id": zero, "unit_price": "0", "discount_amount": "0"}),
            ]
        }
    )
    draft = s["service"].create_draft(s["owner"], s["ledger"], raw)
    edited = replacement(draft, {removed: None}).model_dump(exclude={"reviewed_references"})
    doc = s["service"].update_draft(
        s["owner"],
        s["ledger"],
        draft.id,
        BusinessDraftUpdate(**edited, expected_version=draft.version),
    )
    confirmation = submit(
        s,
        doc,
        BusinessConfirmationCreate(id=uuid4(), expected_version=doc.version),
        "fictional-sparse-original",
    )
    reader = BusinessConfirmationReads(s["engine"])
    detail = reader.get_confirmation(s["owner"], s["ledger"], doc.id)
    assert [(row.id, row.line_no) for row in detail.document.lines] == [(original.id, 1), (zero, 3)]
    assert [p.line_id for p in detail.confirmation.postings] == [original.id]
    header = pattern | dict(
        id=uuid4(),
        confirmation_id=confirmation.id,
        document_id=doc.id,
        idempotency_key="fictional-sparse-revision",
        previous_version=confirmation.document_version,
        document_version=confirmation.document_version + 1,
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        c.execute(
            insert(Fact), line(header, doc) | {"source_line_id": removed if retired else uuid4()}
        )
        saved = c.scalar(
            select(BusinessConfirmation.snapshot).where(BusinessConfirmation.id == confirmation.id)
        )
        assert [r["id"] for r in saved["lines"]] == [str(original.id), str(zero)]
        assert saved == c.scalar(select(func.coinpup_business_source_snapshot(doc.id)))
        c.execute(select(func.coinpup_validate_business_confirmation(confirmation.id)))
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    expected = "ck_document_revision_line_source" if retired else "ck_document_revision_complete"
    assert err.value.orig.diag.constraint_name == expected
    assert snapshot(s) == before
