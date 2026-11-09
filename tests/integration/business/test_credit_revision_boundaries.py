"""Real connection races, quantity limits and reasoned period recovery for credit revisions."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate, BusinessPaymentCreate
from coinpup_api.business.settlement_totals import document_totals, payment_totals
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.schemas import AssetUpdate, CategoryUpdate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import event, func, select, update

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_credit_revision_service import command, failed, replay
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def replacement(sources, *, amount="17.23", tax="2.77", day="2026-11-06", recognition=None):
    return dict(
        replacement=dict(
            credit_date=day,
            reason="Fictional bounded replacement 中文",
            lines=[
                dict(
                    source_line_id=str(source),
                    net_amount=amount,
                    tax_amount=tax,
                    recognition_date=recognition or day,
                )
                for source in sources
            ],
        )
    )


def race(s, attempts):
    barrier, mutex, connections = Barrier(2, timeout=30), Lock(), {}

    def before_lock(connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" not in statement:
            return
        # This event is before the actual lock SQL, not inside the serialized section.
        pid, isolation = connection.exec_driver_sql(
            "SELECT pg_backend_pid(), current_setting('transaction_isolation')"
        ).one()
        with mutex:
            first = pid not in connections
            connections[pid] = isolation
        if first:
            barrier.wait()

    def run(attempt):
        try:
            return attempt()
        except LedgerError as error:
            return error

    event.listen(s["engine"], "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run, attempts))
    finally:
        event.remove(s["engine"], "before_cursor_execute", before_lock)
    assert len(connections) == 2
    assert set(connections.values()) == {"read committed"}
    return results


@pytest.mark.parametrize("mode", ["same_intent", "changed_intent", "same_version"])
def test_two_real_connections_serialize_original_intents_and_expected_versions(drafts, mode):
    s = drafts
    state = group(s)
    sources = state["source_ids"][:2]

    def attempt(index):
        return command(
            s,
            state,
            sources,
            key=f"fictional-version-{index}" if mode == "same_version" else "fictional-race",
            changes=replacement(sources, amount="18" if index and mode != "same_intent" else "17"),
        )

    results = race(s, [lambda: attempt(0), lambda: attempt(1)])
    successes = [item for item in results if not isinstance(item, LedgerError)]
    errors = [item for item in results if isinstance(item, LedgerError)]
    if mode == "same_intent":
        assert not errors and successes[0][-1] == successes[1][-1]
    else:
        assert len(successes) == len(errors) == 1
        assert errors[0].code == (
            "version_conflict" if mode == "same_version" else "idempotency_conflict"
        )
    with s["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(BusinessCreditRevision)) == 1
        assert (
            connection.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.response_status == 200)
            )
            == 1
        )
        assert connection.scalars(
            select(ChangeLog.entity_version)
            .where(
                ChangeLog.entity_type == "business_credits",
                ChangeLog.entity_id == str(state["original"].id),
            )
            .order_by(ChangeLog.entity_version)
        ).all() == [1, 2]
    before = snapshot(s)
    for saved in successes:
        assert replay(s, state, saved) == saved[-1]
    assert snapshot(s) == before


def test_allocation_and_credit_revision_compete_for_the_same_committed_capacity(drafts):
    s = drafts
    state = group(s, count=1, selected=1)
    source = state["source_ids"][0]
    payment_request = BusinessPaymentCreate(
        id=uuid4(),
        direction="receipt",
        party_id=s["party"].id,
        account_id=s["account"].id,
        asset_id="USD",
        amount="100",
        transaction_date="2026-11-06",
        description="Fictional independent race payment",
    )
    payment = pay(s, payment_request)
    allocation = BusinessAllocationCreate(
        id=uuid4(),
        payment_id=payment.id,
        allocation_date="2026-11-06",
        lines=[
            dict(
                document_id=state["original"].document_id,
                document_line_id=source,
                payment_amount="80",
                document_amount="80",
            )
        ],
    )

    def allocate():
        return BusinessSettlementService(s["engine"]).allocate(
            s["owner"], s["ledger"], allocation, "fictional-allocation-race"
        )

    results = race(
        s,
        [
            lambda: command(
                s, state, [source], changes=replacement([source], amount="40", tax="0")
            ),
            allocate,
        ],
    )
    errors = [item for item in results if isinstance(item, LedgerError)]
    assert len(errors) == 1
    assert errors[0].code == (
        "credit_exceeds_open"
        if isinstance(results[0], LedgerError)
        else "allocation_exceeds_document"
    )
    with s["engine"].connect() as connection:
        expected = (
            (Decimal("80"), Decimal("17.23"), Decimal("2.77"))
            if isinstance(results[0], LedgerError)
            else (Decimal(0), Decimal("40"), Decimal(0))
        )
        assert document_totals(connection, s["ledger"], [source])[source] == expected
        assert payment_totals(connection, s["ledger"], [payment.id]).get(
            payment.id, (Decimal(0), Decimal(0))
        ) == (expected[0], Decimal(0))
    before = snapshot(s)
    assert pay(s, payment_request) == payment
    if isinstance(results[0], LedgerError):
        assert allocate() == results[1]
    else:
        assert replay(s, state, results[0]) == results[0][-1]
    assert snapshot(s) == before


@pytest.mark.parametrize("retained", [True, False])
def test_full_document_scale_preserves_every_transition_and_identity(drafts, retained):
    s = drafts
    state = group(s, count=200, selected=200 if retained else 100)
    sources = list(reversed(state["source_ids"])) if retained else state["source_ids"][100:]
    saved = command(s, state, sources)
    response = saved[-1]
    assert len(response.members) == 200
    assert [row.source_line_id for row in response.members if row.action != "cancel"] == sources
    old = {row.source_line_id: row.operation_id for row in state["original"].postings}
    if retained:
        assert all(
            row.action == "correct"
            and row.operation_version == 2
            and row.operation_id == old[row.source_line_id]
            for row in response.members
        )
    else:
        assert [row.action for row in response.members] == ["create"] * 100 + ["cancel"] * 100
        assert all(
            row.operation_id not in old.values() and row.operation_version == 1
            for row in response.members[:100]
        )
    with s["engine"].connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(Journal).where(Journal.sealed.is_(False))
            )
            == 0
        )
        assert connection.scalar(
            select(func.count())
            .select_from(FinancialOperation)
            .where(
                FinancialOperation.kind == "invoice_credit", FinancialOperation.status == "active"
            )
        ) == len(sources)
        totals = document_totals(connection, s["ledger"], state["source_ids"])
        assert set(totals) == set(sources)
    before = snapshot(s)
    assert replay(s, state, saved) == response
    assert credit(s, state["request"]) == state["original"]
    assert snapshot(s) == before


def priced_credit(s, asset, maximum, minimum, *, day="2026-11-06", recognition="2026-11-06"):
    document, confirmation = prepare(
        s,
        changes={"asset_id": asset},
        line_changes=dict(
            quantity="1",
            unit_price=maximum,
            discount_amount="0",
            tax_rate_percent="0",
        ),
    )
    confirmed = submit(s, document, confirmation)
    request = BusinessCreditCreate(
        id=uuid4(),
        document_id=document.id,
        expected_version=confirmed.document_version,
        credit_date=day,
        reason="Fictional precision credit 中文",
        lines=[
            dict(
                source_line_id=document.lines[0].id,
                net_amount=minimum,
                tax_amount="0",
                recognition_date=recognition,
            )
        ],
    )
    original = credit(s, request)
    return dict(original=original, request=request, source_ids=[document.lines[0].id])


@pytest.mark.parametrize("asset,scale", [("USD", 2), ("BTC", 8), ("ETH", 18)])
def test_minimum_to_maximum_and_back_remain_exact_under_trapped_low_context(drafts, asset, scale):
    s = drafts
    maximum, minimum = "9" * 20 + "." + "9" * scale, "0." + "0" * (scale - 1) + "1"
    state = priced_credit(s, asset, maximum, minimum)
    source = state["source_ids"]
    saved = []
    with localcontext() as context:
        context.prec = 1
        for signal in context.traps:
            context.traps[signal] = True
        for version, amount in enumerate((maximum, minimum), 1):
            saved.append(
                command(
                    s,
                    state,
                    source,
                    version=version,
                    changes=replacement(source, amount=amount, tax="0"),
                )
            )
            assert saved[-1][-1].members[0].net_amount == amount + "0" * (18 - scale)
            with s["engine"].connect() as connection:
                assert document_totals(connection, s["ledger"], source)[source[0]] == (
                    Decimal(0),
                    Decimal(amount),
                    Decimal(0),
                )
            assert replay(s, state, saved[-1]) == saved[-1][-1]
    before = snapshot(s)
    assert credit(s, state["request"]) == state["original"]
    for old in saved:
        assert replay(s, state, old) == old[-1]
    assert snapshot(s) == before


@pytest.mark.parametrize("asset,amount", [("USD", "0.001"), ("BTC", "0.000000001")])
def test_asset_precision_rejection_reaches_the_service_and_rolls_back(drafts, asset, amount):
    s = drafts
    state = priced_credit(s, asset, "100", "1")
    failed(
        s,
        "amount_precision",
        lambda: command(
            s,
            state,
            state["source_ids"],
            changes=replacement(state["source_ids"], amount=amount, tax="0"),
        ),
    )


@pytest.mark.parametrize(
    "action,field",
    [
        ("correct", "old_transaction"),
        ("correct", "old_recognition"),
        ("correct", "new_transaction"),
        ("correct", "new_recognition"),
        ("cancel", "old_transaction"),
        ("cancel", "old_recognition"),
    ],
)
def test_each_original_or_replacement_date_needs_explicit_reasoned_reopening(drafts, action, field):
    s = drafts
    closed, opened = "2026-10-20", "2026-11-06"
    state = priced_credit(
        s,
        "USD",
        "100",
        "20",
        day=closed if field == "old_transaction" else opened,
        recognition=closed if field == "old_recognition" else opened,
    )
    sources = state["source_ids"] if action == "correct" else None
    changes = (
        replacement(
            state["source_ids"],
            amount="20",
            tax="0",
            day=closed if field == "new_transaction" else opened,
            recognition=closed if field == "new_recognition" else opened,
        )
        if action == "correct"
        else None
    )
    change_period(s, cutoff=closed)

    def call():
        return command(s, state, sources, key="fictional-reopened-intention", changes=changes)

    failed(s, "period_closed", call)
    reopened = change_period(s, version=2, cutoff=None, action="reopen")
    assert reopened.reason == "Fictional period verification"
    saved = call()
    assert saved[-1].version == 2
    change_period(s, version=3, cutoff=opened)
    before = snapshot(s)
    assert replay(s, state, saved) == saved[-1]
    assert credit(s, state["request"]) == state["original"]
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "disabled,expected",
    [
        ("account", "account_archived"),
        ("link", "account_asset_disabled"),
        ("asset", "asset_disabled"),
        ("category", "reference_archived"),
        ("project", "reference_archived"),
        ("party", "reference_archived"),
    ],
)
def test_each_disabled_reference_rejects_new_use_but_allows_exact_current_cancellation(
    drafts, disabled, expected
):
    s = drafts
    state = group(s)
    sources = state["source_ids"][1:]
    first = command(s, state, sources)
    if disabled in {"account", "link"}:
        # Controls have no public editing endpoint; use the same valid maintenance SQL
        # as the existing control-account tests, under the actual ordered write locks.
        service = BusinessCreditRevisionService(s["engine"])
        with service._transaction(s["owner"], write=True, read_committed=True) as session:
            service._locked_ledger(session, s["owner"], s["ledger"])
            control = session.scalar(
                select(Account).where(
                    Account.ledger_id == s["ledger"], Account.system_key == "receivable.customer"
                )
            )
            if disabled == "account":
                session.execute(
                    update(Account)
                    .where(Account.id == control.id)
                    .values(archived=True, version=control.version + 1)
                )
            else:
                session.execute(
                    update(AccountAsset)
                    .where(AccountAsset.account_id == control.id, AccountAsset.asset_id == "USD")
                    .values(enabled=False)
                )
    elif disabled == "asset":
        s["structure"].update_asset(
            s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False)
        )
    elif disabled == "category":
        s["structure"].update_category(
            s["owner"],
            s["ledger"],
            s["income"].id,
            CategoryUpdate(expected_version=1, archived=True),
        )
    elif disabled == "project":
        s["master"].update_project(
            s["owner"],
            s["ledger"],
            s["project"].id,
            ProjectUpdate(expected_version=1, archived=True),
        )
    else:
        s["master"].update_party(
            s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
        )
    failed(s, expected, lambda: command(s, state, sources, version=2))
    references = snapshot(s)
    cancelled = command(s, state, None, version=2)
    with s["engine"].connect() as connection:
        for member in cancelled[-1].members:
            old_header = (
                connection.execute(
                    select(Journal.__table__).where(Journal.id == member.previous_journal_id)
                )
                .mappings()
                .one()
            )
            reversal = (
                connection.execute(
                    select(Journal.__table__).where(Journal.id == member.reversal_journal_id)
                )
                .mappings()
                .one()
            )
            assert reversal["reverses_journal_id"] == old_header["id"]
            assert reversal["journal_kind"] == "reversal"
            assert all(
                reversal[field] == old_header[field]
                for field in ("transaction_date", "recognition_date", "description")
            )
            rows = []
            for identifier in (member.previous_journal_id, member.reversal_journal_id):
                rows.append(
                    connection.execute(
                        select(JournalLine.__table__)
                        .where(JournalLine.journal_id == identifier)
                        .order_by(JournalLine.line_no)
                    )
                    .mappings()
                    .all()
                )
            assert len(rows[0]) == len(rows[1]) == 2
            for old, new in zip(*rows, strict=True):
                assert new["id"] != old["id"]
                assert dict(new) == dict(old) | dict(
                    id=new["id"],
                    journal_id=member.reversal_journal_id,
                    amount=old["amount"].copy_negate(),
                )
    after = snapshot(s)
    for name in (
        "accounts",
        "account_assets",
        "assets",
        "categories",
        "business_parties",
        "business_projects",
        "entities",
    ):
        assert after[name] == references[name]
    assert replay(s, state, first) == first[-1]
    assert replay(s, state, cancelled) == cancelled[-1]
    assert credit(s, state["request"]) == state["original"]
    assert snapshot(s) == after


def test_archived_entity_rejects_both_new_commands_but_keeps_old_receipts(drafts):
    s = drafts
    state = group(s)
    saved = command(s, state, state["source_ids"][1:])
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    for sources in (state["source_ids"][1:], None):
        failed(s, "entity_archived", lambda sources=sources: command(s, state, sources, version=2))
    before = snapshot(s)
    assert replay(s, state, saved) == saved[-1]
    assert credit(s, state["request"]) == state["original"]
    assert snapshot(s) == before
