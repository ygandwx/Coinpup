"""Real HTTP credit revisions preserve private scope, old receipts and unknown intentions."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from sqlalchemy.exc import SQLAlchemyError

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_credit_service import setup_credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def frozen(s, credit_id, sources, *, version=1, key="fictional-http-revision"):
    action = "cancellations" if sources is None else "corrections"
    body = dict(expected_version=version, reason="Fictional HTTP revision 中文")
    if sources is not None:
        body["replacement"] = dict(
            credit_date="2026-11-06",
            reason="Fictional replacement fact 中文",
            lines=[
                dict(
                    source_line_id=str(source),
                    net_amount="17.23",
                    tax_amount="2.77",
                    recognition_date="2026-11-06",
                )
                for source in sources
            ],
        )
    return (
        f"/api/v1/ledgers/{s['ledger']}/business-credits/{credit_id}/{action}",
        json.dumps(body, ensure_ascii=False, indent=2).encode(),
        {"Idempotency-Key": key, "Content-Type": "application/json"},
    )


def post(client, intention):
    path, raw, headers = intention
    response = client.post(path, content=raw, headers=headers)
    assert response.headers["cache-control"] == "no-store"
    return response


def rejected(s, client, intention, status, code=None):
    before = snapshot(s)
    response = post(client, intention)
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["detail"]["code"] == code
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_real_http_group_revisions_keep_creation_and_every_historical_receipt(
    drafts, authenticated_client, kind
):
    s = drafts
    client, _, _ = authenticated_client
    document, _, _, payload = setup_credit(s, kind, count=3)
    payload = payload.model_copy(update={"lines": payload.lines[:2]})
    base = f"/api/v1/ledgers/{s['ledger']}"
    create = (
        base + "/business-credits",
        payload.model_dump_json().encode(),
        {"Idempotency-Key": "fictional-original-http-credit", "Content-Type": "application/json"},
    )
    original = post(client, create)
    assert original.status_code == 201, original.text
    original = original.json()
    detail_path = base + f"/business-credits/{payload.id}"
    detail = client.get(detail_path).json()
    assert detail["receipt"] == original
    a, _, c = [row.id for row in document.lines]
    correction = frozen(s, payload.id, [a, c])
    response = post(client, correction)
    assert response.status_code == 200, response.text
    corrected = response.json()
    assert (corrected["previous_version"], corrected["version"]) == (1, 2)
    assert [item["action"] for item in corrected["members"]] == ["correct", "create", "cancel"]
    assert [item["operation_version"] for item in corrected["members"]] == [2, 1, 2]
    assert corrected["members"][0]["net_amount"] == "17.230000000000000000"
    assert corrected["members"][2]["net_amount"] is None

    # A newly added member has no original BusinessCreditLine: legacy guards still apply.
    operation = corrected["members"][1]["operation_id"]
    legacy = base + f"/operations/{operation}"
    before = snapshot(s)
    for suffix in ("", "/history"):
        denied = client.get(legacy + suffix)
        assert denied.status_code == 404 and denied.headers["cache-control"] == "no-store"
    replacement = dict(
        kind="expense",
        account_id=str(s["account"].id),
        asset_id="USD",
        amount="1.00",
        transaction_date="2026-11-06",
        recognition_date="2026-11-06",
        splits=[dict(category_id=str(s["expenses"][0].id), amount="1.00")],
    )
    for suffix, body in (
        (
            "corrections",
            dict(expected_version=1, reason="Fictional isolated child", replacement=replacement),
        ),
        ("cancellations", dict(expected_version=1, reason="Fictional isolated child")),
    ):
        denied = client.post(
            legacy + "/" + suffix,
            json=body,
            headers={"Idempotency-Key": "fictional-legacy-" + suffix},
        )
        assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
        assert denied.headers["cache-control"] == "no-store"
    assert snapshot(s) == before

    path, raw, headers = correction
    for change in ("amount_spelling", "member_order", "reason"):
        body = json.loads(raw)
        if change == "amount_spelling":
            body["replacement"]["lines"][0]["net_amount"] = "17.230"
        elif change == "member_order":
            body["replacement"]["lines"].reverse()
        else:
            body["reason"] = "Fictional distinct intention"
        rejected(s, client, (path, json.dumps(body).encode(), headers), 409, "idempotency_conflict")
    rejected(
        s,
        client,
        (path.replace(str(payload.id), str(uuid4())), raw, headers),
        409,
        "idempotency_conflict",
    )
    rejected(s, client, frozen(s, payload.id, None), 409, "idempotency_conflict")
    rejected(
        s,
        client,
        (path, raw, headers | {"Idempotency-Key": "fictional-stale-version"}),
        409,
        "version_conflict",
    )
    cancellation = frozen(s, payload.id, None, version=2, key="fictional-http-cancel")
    response = post(client, cancellation)
    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert (cancelled["previous_version"], cancelled["version"]) == (2, 3)
    assert cancelled["credit_date"] is None and cancelled["replacement_reason"] is None
    assert all(
        item["action"] == "cancel"
        and item["journal_id"] is None
        and item["previous_journal_id"] is not None
        and item["reversal_journal_id"] is not None
        for item in cancelled["members"]
    )
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    for intention, expected, status in (
        (create, original, 201),
        (correction, corrected, 200),
        (cancellation, cancelled, 200),
    ):
        response = post(client, intention)
        assert response.status_code == status and response.json() == expected
    old_detail = client.get(detail_path)
    assert old_detail.status_code == 200 and old_detail.json() == detail
    assert old_detail.headers["cache-control"] == "no-store"
    assert snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_real_http_scope_auth_and_rejected_new_intents_do_not_write(
    drafts, authenticated_client, action
):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s)
    sources = state["source_ids"][:2] if action == "correct" else None
    intention = frozen(s, state["original"].id, sources)
    path, raw, headers = intention
    for changes in (
        {"origin": "https://fictional-attacker.invalid"},
        {"x-csrf-token": "fictional-wrong"},
    ):
        rejected(s, client, (path, raw, headers | changes), 403)
    token = client.cookies.get("coinpup_session")
    client.cookies.clear()
    rejected(s, client, intention, 401)
    client.cookies.set("coinpup_session", token)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional isolated HTTP ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    for foreign_path in (
        path.replace(str(s["ledger"]), str(other.ledger.id)),
        path.replace(str(state["original"].id), str(uuid4())),
    ):
        rejected(s, client, (foreign_path, raw, headers), 404, "not_found")
    body = json.loads(raw)
    invalid = body | {"expected_version": True}
    rejected(s, client, (path, json.dumps(invalid).encode(), headers), 422)
    if action == "correct":
        too_large = deepcopy(body)
        too_large["replacement"]["lines"][0].update(net_amount="100.01", tax_amount="0")
        rejected(
            s, client, (path, json.dumps(too_large).encode(), headers), 422, "credit_exceeds_source"
        )
    change_period(s, cutoff="2026-11-06")
    rejected(s, client, intention, 409, "period_closed")


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_http_unknown_committed_result_and_real_401_replay_only_original_intention(
    drafts, authenticated_client, monkeypatch, action
):
    s = drafts
    client, _, _ = authenticated_client
    state = group(s)
    intention = frozen(
        s, state["original"].id, state["source_ids"][1:] if action == "correct" else None
    )
    original = getattr(BusinessCreditRevisionService, action)
    committed = []

    def lost_response(self, *args, **kwargs):
        committed.append(original(self, *args, **kwargs))
        # The actual transaction committed; only delivery of the successful response fails.
        raise SQLAlchemyError("Fictional lost response after commit")

    before = snapshot(s)
    monkeypatch.setattr(BusinessCreditRevisionService, action, lost_response)
    response = post(client, intention)
    assert (
        response.status_code == 503 and response.json()["detail"]["code"] == "business_unavailable"
    )
    assert len(committed) == 1 and snapshot(s) != before
    monkeypatch.setattr(BusinessCreditRevisionService, action, original)
    saved = snapshot(s)
    token = client.cookies.get("coinpup_session")
    client.cookies.clear()
    rejected(s, client, intention, 401)
    client.cookies.set("coinpup_session", token)
    path, raw, headers = intention
    changed = json.loads(raw) | {"reason": "Fictional different retry"}
    rejected(s, client, (path, json.dumps(changed).encode(), headers), 409, "idempotency_conflict")
    for _ in range(2):
        retry = post(client, intention)
        assert retry.status_code == 200 and retry.json() == committed[0].model_dump(mode="json")
    assert snapshot(s) == saved
