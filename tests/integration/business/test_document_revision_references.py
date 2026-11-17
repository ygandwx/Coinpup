"""Fictional corrected document references keep their individually reviewed evidence."""

from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_references import reviewed_document_references
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentReplacement
from coinpup_api.business.draft_schemas import BusinessDraftUpdate
from coinpup_api.business.schemas import PartyCreate, PartyUpdate, ProjectUpdate
from coinpup_api.ledger.schemas import CategoryUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_document_revision_capacity import confirmed, replacement
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def origin(s, kind="invoice"):
    doc, intent = prepare(s, kind=kind)
    receipt = submit(s, doc, intent)
    return confirmed(s, doc.id), intent, receipt


def pinned(raw, refs):
    data = raw.model_dump() | {
        "reviewed_references": [
            {"kind": kind, "id": identifier, "version": version}
            for kind, identifier, version in refs
        ]
    }
    return ConfirmedDocumentReplacement.model_validate(data)


def read(s, doc, proposed):
    with Session(s["engine"]) as session:
        return reviewed_document_references(session, s["ledger"], doc, proposed)


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_retained_snapshot_is_separate_from_refreshed_master_data_and_original_receipt(
    drafts, kind
):
    s = drafts
    doc, intent, receipt = origin(s, kind)
    old = doc.model_dump()
    category = doc.lines[0].category_id
    s["master"].update_party(
        s["owner"],
        s["ledger"],
        doc.party_id,
        PartyUpdate(expected_version=1, name="Fictional new party"),
    )
    s["master"].update_project(
        s["owner"],
        s["ledger"],
        s["project"].id,
        ProjectUpdate(expected_version=1, name="Fictional new project"),
    )
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        category,
        CategoryUpdate(expected_version=1, name="Fictional new category"),
    )
    before = snapshot(s)
    body = replacement(doc)
    original = read(s, doc, body)
    assert (
        original.issuer_snapshot == doc.issuer_snapshot
        and original.party_snapshot == doc.party_snapshot
    )
    assert original.lines[0].category_snapshot == doc.lines[0].category_snapshot
    assert original.lines[0].project_snapshot == doc.lines[0].project_snapshot
    refreshed = read(
        s,
        doc,
        pinned(
            body,
            [
                ("party", doc.party_id, 2),
                ("category", category, 2),
                ("project", s["project"].id, 2),
            ],
        ),
    )
    assert (
        refreshed.party_snapshot["name"] == "Fictional new party"
        and refreshed.party_snapshot["version"] == 2
    )
    assert refreshed.lines[0].category_snapshot["name"] == "Fictional new category"
    assert refreshed.lines[0].project_snapshot["name"] == "Fictional new project"
    assert (
        "notes" not in refreshed.party_snapshot
        and "notes" not in refreshed.lines[0].project_snapshot
    )
    original.party_snapshot["name"] = "Fictional temporary caller copy"
    original.lines[0].project_snapshot["name"] = "Fictional temporary caller copy"
    assert doc.model_dump() == old and snapshot(s) == before
    assert submit(s, doc, intent) == receipt


@pytest.mark.parametrize("kind", ["party", "category", "project"])
def test_new_or_stale_review_never_implicitly_uses_latest_master(drafts, kind):
    s = drafts
    doc, _, _ = origin(s)
    if kind == "party":
        other = s["master"].create_party(
            s["owner"],
            s["ledger"],
            PartyCreate(id=uuid4(), name="Fictional new party", role="customer"),
        )
        body = replacement(doc, party_id=other.id)
        needed = [("party", other.id, 1)]
    else:
        # New line, same master. Its quantity and description carry no master version.
        body = replacement(doc)
        new = body.lines[0].model_copy(update={"id": uuid4()})
        body = body.model_copy(update={"lines": [new, *body.lines]})
        all_pins = [("category", new.category_id, 1), ("project", new.project_id, 1)]
        needed = [v for v in all_pins if v[0] == kind]
        body = pinned(body, [v for v in all_pins if v[0] != kind])
    other_pins = [(r.kind, r.id, r.version) for r in body.reviewed_references]
    before = snapshot(s)
    for command, code in [
        (body, "document_reference_review_required"),
        (pinned(body, other_pins + [(k, i, 2) for k, i, _ in needed]), "version_conflict"),
    ]:
        with pytest.raises(LedgerError) as err:
            read(s, doc, command)
        assert err.value.code == code
    fact = read(s, doc, pinned(body, other_pins + needed))
    assert [r.source_line_id for r in fact.lines] == [r.id for r in body.lines]
    assert snapshot(s) == before


def test_foreign_scope_archived_references_and_original_fixed_receipt(drafts):
    s = drafts
    doc, intent, receipt = origin(s)
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional other",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )
    outside = s["master"].create_party(
        s["owner"], other, PartyCreate(id=uuid4(), name="Fictional outside", role="both")
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as err:
        read(s, doc, pinned(replacement(doc, party_id=outside.id), [("party", outside.id, 1)]))
    assert err.value.code == "not_found" and snapshot(s) == before
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as err:
        read(s, doc, replacement(doc))
    assert err.value.code == "reference_archived"
    without_project = read(s, doc, replacement(doc, {doc.lines[0].id: {"project_id": None}}))
    assert without_project.lines[0].project_snapshot is None
    assert submit(s, doc, intent) == receipt and snapshot(s) == before


def test_two_hundred_replacement_lines_read_three_master_collections(drafts):
    s = drafts
    doc, _, _ = origin(s)
    body = replacement(doc)
    row = body.lines[0]
    body = pinned(
        body.model_copy(
            update={"lines": [row.model_copy(update={"id": uuid4()}) for _ in range(200)]}
        ),
        [("category", row.category_id, 1), ("project", row.project_id, 1)],
    )
    queries = []

    def capture(_conn, _cursor, statement, *args):
        queries.append(statement)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        facts = read(s, doc, body)
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
    assert len(queries) == 3
    assert [r.source_line_id for r in facts.lines] == [r.id for r in body.lines]


def test_two_retained_lines_can_have_different_saved_snapshots_of_the_same_category(drafts):
    s = drafts
    draft, intent = prepare(s)
    body = replacement(draft)
    first = body.lines[0]
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        first.category_id,
        CategoryUpdate(expected_version=1, name="Fictional reviewed category v2"),
    )
    data = body.model_dump(exclude={"reviewed_references", "lines"})
    updated = s["service"].update_draft(
        s["owner"],
        s["ledger"],
        draft.id,
        BusinessDraftUpdate(
            **data,
            expected_version=draft.version,
            lines=[first, first.model_copy(update={"id": uuid4()})],
        ),
    )
    receipt = submit(s, updated, intent.model_copy(update={"expected_version": updated.version}))
    current = confirmed(s, receipt.document_id)
    assert [row.category_snapshot["version"] for row in current.lines] == [1, 2]
    before = snapshot(s)
    retained = read(s, current, replacement(current))
    refreshed = read(s, current, pinned(replacement(current), [("category", first.category_id, 2)]))
    assert [row.category_snapshot["version"] for row in retained.lines] == [1, 2]
    assert [row.category_snapshot["version"] for row in refreshed.lines] == [2, 2]
    assert confirmed(s, receipt.document_id) == current and snapshot(s) == before
