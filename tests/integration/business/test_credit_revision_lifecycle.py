"""Real complete SQL revisions; request hashing and command replay remain service work."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.schemas import AssetUpdate, EntityUpdate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.credit_transition_fixture import group, transition
from tests.integration.business.test_credit_current_capacity import other_credit
from tests.integration.business.test_credit_fact_validation import (
    copy_posting,
    lock_credit,
    pending_origin,
)
from tests.integration.business.test_credit_revision_receipts import (
    call,
    candidate,
    content,
    save_receipt,
)
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay
from tests.integration.conftest import credit_current_capacity_migration, credit_lifecycle_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def write(connection, state, sources, **kwargs):
    header, rows, response = candidate(connection, state, sources, **kwargs)
    save_receipt(connection, header, response)
    return header, rows, response


def check_group(connection, identifier):
    connection.execute(text("SELECT coinpup_validate_business_credit(:id)"), {"id": identifier})


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_real_commits_retain_all_original_and_revision_receipts_across_rejoin_and_cancel(
    drafts, kind
):
    s = drafts
    state = group(s, kind=kind)
    a, b, c = state["source_ids"]
    original = state["original"]
    first_b = original.postings[1].operation_id
    with s["engine"].connect() as connection:
        original_response = connection.scalar(
            text("SELECT coinpup_business_credit_response(:id)"), {"id": original.id}
        )
    saved = []
    for requested in ([a, c], [b, c], None):
        with s["engine"].begin() as connection:
            header, rows, response = write(connection, state, requested)
            saved.append((header, rows, response))
        with s["engine"].begin() as connection:
            before = content(connection)
            check_group(connection, original.id)
            assert (
                connection.scalar(
                    text("SELECT coinpup_business_credit_response(:id)"), {"id": original.id}
                )
                == original_response
            )
            for historical, _, expected in saved:
                assert call(connection, historical["id"]) == expected
            changes = (
                connection.execute(
                    select(ChangeLog.__table__)
                    .where(
                        ChangeLog.entity_type == "business_credits",
                        ChangeLog.entity_id == str(original.id),
                    )
                    .order_by(ChangeLog.entity_version)
                )
                .mappings()
                .all()
            )
            assert [row["entity_version"] for row in changes] == list(
                range(1, header["group_version"] + 1)
            )
            assert all(
                row["owner_id"] == s["owner"] and row["ledger_id"] == original.ledger_id
                for row in changes
            )
            assert changes[-1]["change_kind"] == ("cancel" if requested is None else "upsert")
            assert credit(s, state["request"]) == original
            assert content(connection) == before
    assert saved[1][1][0]["operation_id"] != first_b
    assert saved[1][1][0]["operation_version"] == 1
    assert saved[1][1][1]["operation_version"] == 2
    with s["engine"].connect() as connection:
        assert document_totals(connection, original.ledger_id, state["source_ids"]) == {}
        owned = {row["operation_id"] for _, rows, _ in saved for row in rows}
        assert set(
            connection.scalars(
                select(FinancialOperation.status).where(FinancialOperation.id.in_(owned))
            )
        ) == {"cancelled"}
    # Cancellation releases capacity; a new independent group can consume the whole source.
    other_credit(s, state, a, net="100", tax="10")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == first_b)
            .values(version=3, status="active", current_journal_id=uuid4())
        )
    assert failure.value.orig.diag.constraint_name == "ck_posting_immutable"
    assert snapshot(s) == before
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert credit(s, state["request"]) == original
    with s["engine"].connect() as connection:
        for header, _, expected in saved:
            assert call(connection, header["id"]) == expected
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "selected,positions", [(200, tuple(range(200))), (100, tuple(range(100, 200)))]
)
def test_actual_document_limit_commits_without_dropping_transition_members(
    drafts, selected, positions
):
    s = drafts
    state = group(s, count=200, selected=selected)
    with s["engine"].begin() as connection:
        header, members, expected = write(
            connection, state, [state["source_ids"][i] for i in positions]
        )
    with s["engine"].connect() as connection:
        assert call(connection, header["id"]) == expected
        assert len(members) == 200


@pytest.mark.parametrize("target", ["original", "created"])
@pytest.mark.parametrize("with_reversal", [False, True])
def test_exact_isolated_cancellation_cannot_advance_owned_operation_without_group_transition(
    drafts, target, with_reversal
):
    s = drafts
    state = group(s)
    if target == "created":
        with s["engine"].begin() as connection:
            write(connection, state, [state["source_ids"][2]])
    prior = next(iter(state["current"].values()))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        lock_credit(connection, state["original"])
        connection.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == prior["operation_id"])
            .values(version=prior["operation_version"] + 1, status="cancelled")
        )
        if with_reversal:
            copy_posting(
                connection,
                prior["journal_id"],
                dict(
                    id=uuid4(),
                    operation_version=prior["operation_version"] + 1,
                    journal_kind="reversal",
                    reverses_journal_id=prior["journal_id"],
                    revision_reason="Fictional orphan cancellation",
                ),
                reverse=True,
            )
        # Only the new owner trigger is forced; unrelated deferred checks cannot satisfy this test.
        connection.exec_driver_sql("SET CONSTRAINTS zz_tr_credit_operations_complete IMMEDIATE")
        pytest.fail("An owned operation advanced outside its complete group")
    assert failure.value.orig.diag.constraint_name == (
        "ck_business_credit_shape" if target == "original" else "ck_credit_revision_shape"
    )
    assert failure.value.orig.diag.message_primary == (
        "Business credit line shape is invalid"
        if target == "original"
        else "Credit operation state is inconsistent"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["member", "dimension", "reversal", "receipt", "old_receipt", "after_cancel"]
)
def test_full_group_rejects_broken_transition_and_rolls_back_every_table(drafts, fault):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        options = {}
        if fault == "reversal":
            options["journal_changes"] = {"revision_reason": "Fictional inconsistent reason"}
        elif fault == "after_cancel":
            write(connection, state, None)
        elif fault == "dimension":
            options["posting_line_changes"] = {"project_id": None}
        if fault == "member":
            transition(
                connection,
                state,
                [state["source_ids"][0]],
                member_changes=lambda row: {"omit": row["member_no"] == 1},
            )
        else:
            header, _, expected = candidate(connection, state, [state["source_ids"][0]], **options)
            if fault in {"receipt", "old_receipt"}:
                expected["members"][0]["previous_net_amount"] = "0"
            save_receipt(connection, header, expected)
            if fault == "old_receipt":
                write(connection, state, [state["source_ids"][0]])
        check_group(connection, state["original"].id)
        pytest.fail("The full group validator accepted a broken transition")
    assert failure.value.orig.sqlstate == "23514"
    assert failure.value.orig.diag.constraint_name == (
        "ck_business_credit_shape" if fault == "dimension" else "ck_credit_revision_shape"
    )
    assert snapshot(s) == before


def test_composed_validation_still_rejects_an_invalid_permanent_creation_receipt(drafts):
    s = drafts
    state = group(s, count=1, selected=1)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        identifier = pending_origin(
            connection, state["original"], receipt_changes={"response_status": 200}
        )
        check_group(connection, identifier)
        pytest.fail("The original 201 receipt proof was bypassed")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("component", ["net", "tax"])
def test_latest_group_counts_other_groups_when_manually_entered_split_changes(drafts, component):
    s = drafts
    state = group(s, count=1, selected=1)
    source = state["source_ids"][0]
    other_credit(
        s,
        state,
        source,
        net="82.77" if component == "net" else "0",
        tax="7.23" if component == "tax" else "0",
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        net, tax = ("17.24", "2.76") if component == "net" else ("17.22", "2.78")
        write(
            connection,
            state,
            [source],
            member_changes=lambda row: dict(net_amount=net, tax_amount=tax),
        )
        check_group(connection, state["original"].id)
        pytest.fail("The revised group exceeded shared net/tax capacity")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert failure.value.orig.diag.message_primary == "Business credit line shape is invalid"
    assert snapshot(s) == before


@pytest.mark.parametrize("applied,exceeds", [("80", False), ("80.01", True)])
def test_revised_new_source_shares_gross_capacity_with_existing_allocation(
    drafts, applied, exceeds
):
    s = drafts
    state = group(s)
    a, _, c = state["source_ids"]
    original = state["original"]
    other_credit(s, state, c, net="10")
    payload = BusinessPaymentCreate(
        id=uuid4(),
        direction="receipt",
        party_id=s["party"].id,
        account_id=s["account"].id,
        asset_id="USD",
        amount="100",
        transaction_date=original.credit_date,
        description="Fictional revised gross capacity",
        initial_allocation=dict(
            id=uuid4(),
            allocation_date=original.credit_date,
            lines=[
                dict(
                    document_id=original.document_id,
                    document_line_id=c,
                    payment_amount=applied,
                    document_amount=applied,
                )
            ],
        ),
    )
    paid = pay(s, payload)
    before = snapshot(s)
    with pytest.raises(IntegrityError) if exceeds else nullcontext() as failure:
        with s["engine"].begin() as connection:
            write(connection, state, [a, c])
            check_group(connection, original.id)
            if exceeds:
                pytest.fail("The current group exceeded allocated gross capacity")
    if exceeds:
        assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
        assert failure.value.orig.diag.message_primary == "Business credit line shape is invalid"
        assert snapshot(s) == before
    else:
        before = snapshot(s)
        assert pay(s, payload) == paid
        assert snapshot(s) == before


@pytest.mark.parametrize("cutoff", ["2026-10-20", "2026-11-06"])
def test_revision_still_requires_both_original_and_replacement_dates_to_be_open(drafts, cutoff):
    s = drafts
    state = group(s)
    change_period(s, cutoff=cutoff)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        write(connection, state, list(state["current"]))
    assert failure.value.orig.diag.constraint_name == "ck_journal_period_closed"
    assert snapshot(s) == before


def test_empty_layer_restores_all_previous_sql_and_rejects_history_downgrade(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    migration = credit_lifecycle_migration()
    predecessor = credit_current_capacity_migration()["_CREDIT"]
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        latest = definitions(connection)
        migration["downgrade"]()
        old = definitions(connection)
        assert (
            connection.scalar(
                text(
                    "SELECT prosrc FROM pg_proc WHERE oid="
                    "'public.coinpup_validate_business_credit(uuid)'::regprocedure"
                )
            )
            == predecessor.split("AS $$", 1)[1].rsplit("$$", 1)[0]
        )
        for name in (
            "coinpup_validate_initial_journal",
            "coinpup_validate_operation_history",
            "coinpup_validate_reversal",
            "coinpup_validate_posting_shape",
            "coinpup_business_credit_response",
        ):
            assert [x for x in old[0] if name + "(" in x.split("\n", 1)[0]] == [
                x for x in latest[0] if name + "(" in x.split("\n", 1)[0]
            ]
        migration["upgrade"]()
        assert definitions(connection) == latest
    assert snapshot(s) == before
    with s["engine"].begin() as connection:
        write(connection, state, None)
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as failure,
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
