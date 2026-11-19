"""Use real document correction, current and history routes at the 401-reference boundary."""

from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.schemas import ProjectCreate
from coinpup_api.ledger.schemas import CategoryCreate

from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction
from tests.integration.business.test_document_revision_http import frozen, post
from tests.integration.business.test_document_revision_references import origin, pinned
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def selected_masters(s, doc):
    original = replacement(doc)
    lines = []
    pins = [("party", doc.party_id, 1)]
    pairs = []
    for position in range(200):
        category_id = uuid4()
        project_id = category_id if position == 199 else uuid4()
        category = s["structure"].create_category(
            s["owner"],
            s["ledger"],
            CategoryCreate(
                id=category_id,
                kind="income" if doc.document_kind == "invoice" else "expense",
                name=f"Fictional category {position}",
            ),
        )
        project = s["master"].create_project(
            s["owner"],
            s["ledger"],
            ProjectCreate(id=project_id, name=f"Fictional project {position}"),
        )
        pairs.append((category, project))
        pins.extend(
            [("category", category.id, category.version), ("project", project.id, project.version)]
        )
        lines.append(
            original.lines[0].model_copy(
                update={
                    "id": uuid4(),
                    "category_id": category.id,
                    "project_id": project.id,
                }
            )
        )
    proposed = original.model_copy(update={"lines": lines})
    return proposed, pins, pairs


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_all_selected_master_versions_on_the_wire_remain_fixed_after_cancellation(
    drafts, authenticated_client, kind
):
    s = drafts
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    doc, _, _ = origin(s, kind)
    path = f"/api/v1/ledgers/{s['ledger']}/business-documents/{doc.id}"
    original_response = client.get(path + "/confirmation")
    assert original_response.status_code == 200
    original = original_response.content
    proposed, pins, pairs = selected_masters(s, doc)
    assert len(set((kind, identifier) for kind, identifier, _ in pins)) == 401
    final_kind, final_id, final_version = pins[-1]

    def intent(reviewed, key):
        payload = correction(doc).model_copy(update={"replacement": pinned(proposed, reviewed)})
        return frozen(s, doc, payload, key)

    before = snapshot(s)
    key = "fictional-401-explicit-master-versions"
    for reviewed, code in [
        (pins[:-1], "document_reference_review_required"),
        (pins[:-1] + [(final_kind, final_id, final_version + 1)], "version_conflict"),
    ]:
        rejected = post(client, intent(reviewed, key))
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == code
        assert snapshot(s) == before

    reviewed = intent(pins, key)
    response = post(client, reviewed)
    assert response.status_code == 200, response.text
    fixed = response.json()
    assert fixed["version"] == doc.version + 1
    now = client.get(path + "/current")
    assert now.status_code == 200
    current = now.json()
    assert current["revision_id"] == fixed["revision_id"]
    assert len(current["postings"]) == 200
    assert len(current["document"]["lines"]) == 200
    for row, planned, (cat, project) in zip(
        current["document"]["lines"], proposed.lines, pairs, strict=True
    ):
        assert row["id"] == str(planned.id)
        for field, source in [("category_snapshot", cat), ("project_snapshot", project)]:
            saved = row[field]
            assert (saved["id"], saved["name"], saved["version"]) == (
                str(source.id),
                source.name,
                source.version,
            )

    detail_path = path + "/revisions/" + fixed["revision_id"]
    detail = client.get(detail_path)
    assert detail.status_code == 200 and detail.json() == fixed
    cancelled = post(
        client,
        frozen(
            s,
            doc,
            ConfirmedDocumentCancellation(
                expected_version=fixed["version"],
                reason="Fictional reversal after all selected master versions",
            ),
            "fictional-401-cancel",
        ),
    )
    assert cancelled.status_code == 200, cancelled.text
    terminal = client.get(path + "/current").json()
    assert terminal["status"] == "cancelled" and terminal["version"] == doc.version + 2
    after = snapshot(s)
    assert post(client, reviewed).content == response.content
    assert client.get(detail_path).content == detail.content
    assert client.get(path + "/confirmation").content == original
    assert [r["action"] for r in client.get(path + "/history").json()] == [
        "confirm",
        "correct",
        "cancel",
    ]
    assert snapshot(s) == after
