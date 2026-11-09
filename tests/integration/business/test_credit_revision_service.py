"""Actual credit command commits, frozen intentions and all-table atomic rollback."""

import json
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business import credit_revisions
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
)
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.models import Account, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import event, func, select

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_credit_current_capacity import other_credit
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def command(s, state, sources, *, version=1, key=None, changes=None, owner=None, ledger=None):
    data = dict(expected_version=version, reason="Fictional correction reason 中文")
    action = "cancel" if sources is None else "correct"
    if sources is not None:
        data["replacement"] = dict(
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
    data.update(changes or {})
    payload = (
        CreditCorrectionCreate if sources is not None else CreditCancellationCreate
    ).model_validate(data)
    raw = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False).encode()
    key = key or f"fictional-{action}-{version}"
    invoke = getattr(BusinessCreditRevisionService(s["engine"]), action)
    result = invoke(
        owner or s["owner"], ledger or s["ledger"], state["original"].id, payload, key, raw_body=raw
    )
    return action, payload, key, raw, result


def replay(s, state, saved):
    action, payload, key, raw, _ = saved
    return getattr(BusinessCreditRevisionService(s["engine"]), action)(
        s["owner"], s["ledger"], state["original"].id, payload, key, raw_body=raw
    )


def failed(s, expected, call):
    before = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        call()
    assert failure.value.code == expected
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_atomic_complete_revisions_rejoin_cancel_and_permanent_replay(drafts, kind):
    s = drafts
    state = group(s, kind=kind)
    a, b, c = state["source_ids"]
    original = state["original"]
    old_a, old_b = [item.operation_id for item in original.postings]
    saved = []
    for version, sources in enumerate(([a, c], [b, c], None), 1):
        saved.append(command(s, state, sources, version=version))
        response = saved[-1][-1]
        assert (response.previous_version, response.version) == (version, version + 1)
        assert response.previous_credit_date == (
            original.credit_date if version == 1 else saved[-2][-1].credit_date
        )
        assert [item.source_line_id for item in response.members if item.action != "cancel"] == (
            sources or []
        )
        assert all(
            item.reversal_journal_id is not None
            for item in response.members
            if item.action != "create"
        )
        with s["engine"].connect() as connection:
            assert all(
                value == 0
                for value in connection.scalars(
                    select(func.sum(JournalLine.amount)).group_by(JournalLine.asset_id)
                )
            )
            role = "income" if kind == "invoice" else "expense"
            classified = connection.scalar(
                select(func.sum(JournalLine.amount)).where(JournalLine.role == role)
            )
            assert classified == Decimal(330 if sources is None else 290) * (
                -1 if kind == "invoice" else 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(JournalLine)
                    .join(Account, Account.id == JournalLine.account_id)
                    .where(Account.account_class == "money")
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count()).select_from(Journal).where(Journal.sealed.is_(False))
                )
                == 0
            )
            changes = connection.execute(
                select(ChangeLog.entity_version, ChangeLog.change_kind)
                .where(
                    ChangeLog.entity_type == "business_credits",
                    ChangeLog.entity_id == str(original.id),
                )
                .order_by(ChangeLog.entity_version)
            ).all()
            assert [item.entity_version for item in changes] == list(range(1, version + 2))
            assert changes[-1].change_kind == ("cancel" if sources is None else "upsert")
        before = snapshot(s)
        assert credit(s, state["request"]) == original
        for historical in saved:
            assert replay(s, state, historical).model_dump(mode="json") == historical[
                -1
            ].model_dump(mode="json")
        assert snapshot(s) == before
    assert saved[0][-1].members[0].operation_id == old_a
    assert saved[1][-1].members[0].operation_id != old_b
    assert [item.operation_version for item in saved[1][-1].members[:2]] == [1, 2]
    assert all(
        item.net_amount is None and item.previous_net_amount is not None
        for item in saved[-1][-1].members
    )
    with s["engine"].connect() as connection:
        assert document_totals(connection, s["ledger"], state["source_ids"]) == {}
        assert set(
            connection.scalars(
                select(FinancialOperation.status).where(FinancialOperation.kind == f"{kind}_credit")
            )
        ) == {"cancelled"}
    failed(s, "version_conflict", lambda: command(s, state, [a], version=1, key="fictional-stale"))
    failed(s, "credit_cancelled", lambda: command(s, state, None, version=4))
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert credit(s, state["request"]) == original
    for historical in saved:
        assert replay(s, state, historical) == historical[-1]
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["raw_amount", "action", "path"])
def test_reused_key_rejects_a_different_original_intention(drafts, fault):
    s = drafts
    state = group(s)
    saved = command(s, state, state["source_ids"][:2])
    action, payload, key, raw, _ = saved
    credit_id = state["original"].id
    if fault == "raw_amount":
        altered = json.loads(raw)
        altered["replacement"]["lines"][0]["net_amount"] = "17.230"
        payload = CreditCorrectionCreate.model_validate(altered)
        raw = json.dumps(altered).encode()
    elif fault == "action":
        action = "cancel"
        payload = CreditCancellationCreate(expected_version=1, reason=payload.reason)
        raw = payload.model_dump_json().encode()
    else:
        credit_id = other_credit(s, state, state["source_ids"][0])[0].id
    failed(
        s,
        "idempotency_conflict",
        lambda: getattr(BusinessCreditRevisionService(s["engine"]), action)(
            s["owner"], s["ledger"], credit_id, payload, key, raw_body=raw
        ),
    )


@pytest.mark.parametrize("point", ["second_member", "before_receipt"])
def test_interruption_rolls_back_every_member_head_receipt_and_notification(
    drafts, monkeypatch, point
):
    s = drafts
    state = group(s)
    before = snapshot(s)
    entered = []
    if point == "before_receipt":
        real = credit_revisions.response_from_rows

        def fail(session, identifier):
            entered.append(real(session, identifier))
            raise RuntimeError("Fictional interruption before receipt")

        monkeypatch.setattr(credit_revisions, "response_from_rows", fail)

    def before_execute(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO business_credit_revision_members"):
            entered.append(statement)
            if len(entered) == 2:
                raise RuntimeError("Fictional interruption during second member")

    if point == "second_member":
        event.listen(s["engine"], "before_cursor_execute", before_execute)
    try:
        with pytest.raises(RuntimeError, match="Fictional interruption"):
            command(s, state, [state["source_ids"][0], state["source_ids"][2]])
    finally:
        if point == "second_member":
            event.remove(s["engine"], "before_cursor_execute", before_execute)
    assert len(entered) == (2 if point == "second_member" else 1)
    assert snapshot(s) == before
    if point == "before_receipt":
        monkeypatch.setattr(credit_revisions, "response_from_rows", real)
    saved = command(s, state, [state["source_ids"][0], state["source_ids"][2]])
    assert replay(s, state, saved) == saved[-1]


@pytest.mark.parametrize("component", ["net", "tax", "gross"])
def test_replacement_excludes_own_current_amounts_but_preserves_shared_capacity(drafts, component):
    s = drafts
    state = group(s)
    source = state["source_ids"][0]
    if component == "gross":
        original = state["original"]
        pay(
            s,
            BusinessPaymentCreate(
                id=uuid4(),
                direction="receipt",
                party_id=s["party"].id,
                account_id=s["account"].id,
                asset_id="USD",
                amount="90",
                transaction_date=original.credit_date,
                description="Fictional committed settlement",
                initial_allocation=dict(
                    id=uuid4(),
                    allocation_date=original.credit_date,
                    lines=[
                        dict(
                            document_id=original.document_id,
                            document_line_id=source,
                            payment_amount="90",
                            document_amount="90",
                        )
                    ],
                ),
            ),
        )
        net, tax = "17.24", "2.77"
    else:
        other_credit(
            s,
            state,
            source,
            net="82.77" if component == "net" else "0",
            tax="0" if component == "net" else "7.23",
        )
        net, tax = ("17.24", "2.77") if component == "net" else ("17.23", "2.78")
    saved = command(s, state, [source])
    replacement = saved[1].replacement.model_dump(mode="json")
    replacement["lines"][0].update(net_amount=net, tax_amount=tax)
    failed(
        s,
        "credit_exceeds_open" if component == "gross" else "credit_exceeds_source",
        lambda: command(s, state, [source], version=2, changes={"replacement": replacement}),
    )
    assert replay(s, state, saved) == saved[-1]


@pytest.mark.parametrize("scope", ["owner", "ledger", "source"])
def test_real_scope_boundaries_preserve_all_tables(drafts, scope):
    s = drafts
    state = group(s)
    owner, ledger, sources = s["owner"], s["ledger"], state["source_ids"][:1]
    if scope == "owner":
        owner = uuid4()
    elif scope == "ledger":
        entity = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional unrelated revision ledger",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        ledger = entity.ledger.id
    else:
        document, payload = prepare(s)
        submit(s, document, payload, key="fictional-other-source-confirm")
        sources = [document.lines[0].id]
    failed(s, "not_found", lambda: command(s, state, sources, owner=owner, ledger=ledger))


def test_cancel_reverses_disabled_old_references_without_new_use(drafts):
    s = drafts
    state = group(s)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"], s["ledger"], s["income"].id, CategoryUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    failed(s, "reference_archived", lambda: command(s, state, state["source_ids"][:2]))
    saved = command(s, state, None)
    assert all(item.action == "cancel" and item.journal_id is None for item in saved[-1].members)
    before = snapshot(s)
    assert replay(s, state, saved) == saved[-1]
    assert credit(s, state["request"]) == state["original"]
    assert snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_original_closed_recognition_date_rejects_entire_revision(drafts, action):
    s = drafts
    state = group(s)
    change_period(s, cutoff="2026-10-20")
    failed(
        s,
        "period_closed",
        lambda: command(s, state, state["source_ids"][:2] if action == "correct" else None),
    )
