"""Real owner-scoped evidence reads, including archives and unchanged business rows."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.candidates import completion_from_result
from coinpup_api.ocr.draft_reads import DraftReadService
from coinpup_api.ocr.runtime import processing_configuration
from sqlalchemy import event

from tests.integration.ocr.test_ocr_queue import archive, create, finances
from tests.integration.ocr.test_ocr_queue import queue_structure as queue_structure
from tests.integration.ocr.test_ocr_schema import ocr_structure as ocr_structure
from tests.integration.ocr.test_ocr_schema import rows
from tests.unit.ocr.test_candidates import statement

pytestmark = pytest.mark.integration


@pytest.fixture
def drafts(queue_structure):
    s = queue_structure
    s["configuration"]["processing"] = processing_configuration()
    s["job"] = create(s)
    s["recognition"] = statement("10.00", "10.00", "bad", ocr=(0,))
    lease = s["queue"].claim(s["owner"])
    s["receipt"] = s["queue"].finish(lease, completion_from_result(s["recognition"]))
    s["reader"] = DraftReadService(s["engine"])
    return s


def test_reads_page_metadata_and_preserve_full_private_evidence_after_archive(drafts):
    s = drafts
    service, owner, ledger = s["reader"], s["owner"], s["ledgers"][0]
    for archived in (False, True):
        if archived:
            archive(s, "file")
            archive(s, "entity")
        before = rows(s["engine"]), finances(s["engine"])
        listed = service.list_drafts(owner, ledger, job_id=s["job"].id)
        assert len(listed) == 3
        assert service.list_drafts(owner, ledger, limit=1, offset=1) == listed[1:2]
        assert service.list_drafts(owner, ledger, status="ignored") == []
        assert service.list_drafts(owner, ledger, job_id=uuid4()) == []
        for item in listed:
            detail = service.get_draft(owner, ledger, item.id)
            assert detail.recognition == s["recognition"]
            assert detail.selection == s["configuration"]["processing"]["selection"]
            assert detail.file_id == s["files"][0]
            assert detail.fields["confirmed"] == []
            assert all(field["requires_confirmation"] for field in detail.fields["review"])
            assert (
                not {"lease_token", "blob_key", "configuration", "created_by"}
                & detail.model_dump().keys()
            )
        assert (rows(s["engine"]), finances(s["engine"])) == before


def test_reads_reject_unknown_owner_and_cross_ledger_identifiers(drafts):
    s = drafts
    identifier = s["receipt"].result.draft_ids[0]
    for owner, ledger in ((uuid4(), s["ledgers"][0]), (s["owner"], s["ledgers"][1])):
        with pytest.raises(LedgerError) as caught:
            s["reader"].get_draft(owner, ledger, identifier)
        assert caught.value.code == "not_found"
    assert s["reader"].list_drafts(s["owner"], s["ledgers"][1], job_id=s["job"].id) == []


def test_list_query_count_is_constant_and_selects_no_private_json(drafts):
    s, queries = drafts, []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        counts = []
        for limit in (1, 3):
            queries.clear()
            assert len(s["reader"].list_drafts(s["owner"], s["ledgers"][0], limit=limit)) == limit
            counts.append(len(queries))
            sql = " ".join(queries).lower()
            assert "for update" not in sql and "pg_advisory" not in sql
            assert not any(
                f"ocr_drafts.{name}" in sql for name in ("recognized", "evidence", "fields")
            )
            assert "ocr_jobs.result" not in sql and "ocr_jobs.configuration" not in sql
        assert counts == [5, 5]  # Snapshot setup, owner, ledger, entity, one metadata query.
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
