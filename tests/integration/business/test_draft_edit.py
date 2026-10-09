"""Whole-draft edits preserve identity and isolate stale or interrupted requests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.business.draft_schemas import (
    BusinessDraftArchive,
    BusinessDraftLineInput,
    BusinessDraftResponse,
    BusinessDraftUpdate,
    DraftHeaderInput,
)
from coinpup_api.business.models import BusinessDocumentLine
from coinpup_api.business.schemas import PartyCreate, PartyUpdate, ProjectUpdate
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import select

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import (
    financial_counts,
)
from tests.integration.ledger.test_posting_service_database import (
    ledger_setup as ledger_setup,
)

pytestmark = pytest.mark.integration


def command(row, **changes):
    body = {field: getattr(row, field) for field in DraftHeaderInput.model_fields}
    body["lines"] = [
        {field: getattr(line, field) for field in BusinessDraftLineInput.model_fields}
        for line in row.lines
    ]
    return BusinessDraftUpdate(**(body | dict(expected_version=row.version) | changes))


def save(s, row, **changes):
    return s["service"].update_draft(s["owner"], s["ledger"], row.id, command(row, **changes))


def create(s):
    return s["service"].create_draft(s["owner"], s["ledger"], s["payload"]())


def test_append_remove_restore_and_reorder_keep_stable_line_identity(drafts):
    s = drafts
    original = create(s)
    financial = financial_counts(s["engine"])
    first = command(original).lines[0].model_dump()
    second, third = first | dict(id=uuid4()), first | dict(id=uuid4())
    row = save(s, original, lines=[first, second, third])
    assert row.version == 2 and [line.version for line in row.lines] == [1, 1, 1]
    row = save(s, row, lines=[first | dict(quantity="4.00"), third])
    assert row.version == 3 and [line.line_no for line in row.lines] == [1, 3]
    assert [line.version for line in row.lines] == [2, 1]
    with s["engine"].connect() as c:
        archived = (
            c.execute(
                select(BusinessDocumentLine.__table__).where(
                    BusinessDocumentLine.id == second["id"],
                )
            )
            .mappings()
            .one()
        )
    assert archived["archived"] and archived["version"] == 2
    fourth = first | dict(id=uuid4())
    row = save(s, row, lines=[third, second, first | dict(quantity="4.00"), fourth])
    assert row.version == 4 and [line.line_no for line in row.lines] == [1, 2, 3, 4]
    assert [line.version for line in row.lines] == [2, 3, 1, 1]
    stable = row.lines
    row = save(s, row, lines=list(reversed(command(row).lines)))
    assert row.version == 5 and row.lines == stable
    assert financial_counts(s["engine"]) == financial


def test_snapshots_change_only_on_explicit_refresh_or_reference_replacement(drafts):
    s = drafts
    original = create(s)
    s["master"].update_party(
        s["owner"],
        s["ledger"],
        s["party"].id,
        PartyUpdate(expected_version=1, name="Fictional renamed party"),
    )
    s["master"].update_project(
        s["owner"],
        s["ledger"],
        s["project"].id,
        ProjectUpdate(expected_version=1, name="Fictional renamed project"),
    )
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["income"].id,
        CategoryUpdate(expected_version=1, name="Fictional renamed category"),
    )
    s["structure"].update_entity(
        s["owner"],
        s["entity"].id,
        EntityUpdate(expected_version=1, name="Fictional renamed issuer"),
    )
    same = save(s, original)
    assert same.issuer_snapshot == original.issuer_snapshot
    assert same.party_snapshot == original.party_snapshot and same.lines == original.lines
    refreshed = save(s, same, refresh_snapshots=True)
    assert refreshed.version == 3 and refreshed.lines[0].version == 2
    for value in (
        refreshed.issuer_snapshot,
        refreshed.party_snapshot,
        refreshed.lines[0].category_snapshot,
        refreshed.lines[0].project_snapshot,
    ):
        assert value["version"] == 2 and "renamed" in value["name"]
    party = s["master"].create_party(
        s["owner"],
        s["ledger"],
        PartyCreate(
            id=uuid4(),
            name="Fictional replacement supplier",
            role="supplier",
        ),
    )
    lines = command(refreshed).model_dump()["lines"]
    lines[0].update(category_id=s["expenses"][0].id, project_id=None)
    replaced = save(
        s, refreshed, document_kind="bill", party_id=party.id, asset_id="EUR", lines=lines
    )
    assert replaced.asset_id == replaced.lines[0].asset_id == "EUR"
    assert replaced.party_snapshot["id"] == str(party.id)
    assert replaced.lines[0].category_snapshot["id"] == str(s["expenses"][0].id)
    assert replaced.lines[0].project_snapshot is None
    assert replaced.lines[0].id == original.lines[0].id


def test_concurrent_parent_versions_and_unknown_retry_never_overwrite(drafts):
    s = drafts
    original = create(s)

    def edit(note):
        try:
            return save(s, original, notes=note).notes
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["Fictional first", "Fictional second"]))
    assert results.count("version_conflict") == 1
    current = s["service"].get_draft(s["owner"], s["ledger"], original.id)
    assert current.version == 2 and current.notes in results
    assert current.lines == original.lines
    before = snapshot(s)
    assert edit("Fictional stale intent") == "version_conflict"
    assert snapshot(s) == before


def test_failure_after_full_flush_rolls_back_parent_lines_and_notifications(drafts, monkeypatch):
    s = drafts
    original = create(s)
    before = snapshot(s)
    replacement = command(original).lines[0].model_dump() | dict(id=uuid4())

    def fail(*args, **kwargs):
        raise RuntimeError("Fictional interrupted response")

    monkeypatch.setattr(BusinessDraftResponse, "__init__", fail)
    with pytest.raises(RuntimeError, match="Fictional interrupted response"):
        save(s, original, lines=[replacement], notes="Fictional change")
    assert snapshot(s) == before


def test_foreign_document_lines_and_owned_read_boundaries(drafts):
    s = drafts
    original, other = create(s), create(s)
    foreign_ledger = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional foreign ledger",
                base_asset_id="USD",
            ),
        )
        .ledger.id
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        save(s, original, lines=command(other).lines)
    assert error.value.code == "not_found"
    for owner, ledger in ((uuid4(), s["ledger"]), (s["owner"], foreign_ledger)):
        for action in (
            lambda owner=owner, ledger=ledger: s["service"].get_draft(owner, ledger, original.id),
            lambda owner=owner, ledger=ledger: s["service"].update_draft(
                owner, ledger, original.id, command(original)
            ),
            lambda owner=owner, ledger=ledger: s["service"].set_draft_archived(
                owner, ledger, original.id, BusinessDraftArchive(expected_version=1, archived=True)
            ),
        ):
            with pytest.raises(LedgerError) as error:
                action()
            assert error.value.code == "not_found"
    assert snapshot(s) == before


def test_archive_restore_preserves_lines_even_after_references_become_unavailable(drafts):
    s = drafts
    original = create(s)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    archived = s["service"].set_draft_archived(
        s["owner"],
        s["ledger"],
        original.id,
        BusinessDraftArchive(expected_version=1, archived=True),
    )
    assert archived.archived and archived.version == 2 and archived.lines == original.lines
    assert s["service"].get_draft(s["owner"], s["ledger"], original.id) == archived
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        save(s, archived)
    assert error.value.code == "draft_archived" and snapshot(s) == before
    restored = s["service"].set_draft_archived(
        s["owner"],
        s["ledger"],
        original.id,
        BusinessDraftArchive(expected_version=2, archived=False),
    )
    assert not restored.archived and restored.version == 3 and restored.lines == original.lines
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        s["service"].set_draft_archived(
            s["owner"],
            s["ledger"],
            original.id,
            BusinessDraftArchive(expected_version=2, archived=True),
        )
    assert error.value.code == "version_conflict" and snapshot(s) == before


def test_invalid_full_edit_is_atomic_and_zero_lines_are_explicit(drafts):
    s = drafts
    original = create(s)
    before = snapshot(s)
    lines = command(original).model_dump()["lines"]
    lines[0]["unit_price"] = "0.001"
    with pytest.raises(LedgerError) as error:
        save(s, original, lines=lines, notes="Fictional rejected")
    assert error.value.code == "amount_precision" and snapshot(s) == before
    empty = save(s, original, lines=[])
    assert empty.version == 2 and empty.lines == [] and empty.total_amount == "0.00"
    revived = save(s, empty, lines=command(original).lines)
    assert revived.lines[0].line_no == 1 and revived.lines[0].version == 3
