"""Explicit fictional historical JSON proofs; revisions still cannot commit through the gate."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_revision_member_models import (
    BusinessCreditRevisionMember as Member,
)
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, Entity, FinancialOperation
from coinpup_api.models import Base
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.credit_transition_fixture import group, transition
from tests.integration.business.test_credit_fact_validation import (
    copy_posting,
    pending_origin,
    stored,
)
from tests.integration.business.test_credit_projections import gated
from tests.integration.business.test_credit_revision_member_storage import member
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_receipt_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_schema import write_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
FUNCTIONS = (
    "coinpup_business_credit_revision_response",
    "coinpup_validate_credit_revision_receipt",
)


def call(connection, identifier, *, validate=False):
    return connection.scalar(text(f"SELECT {FUNCTIONS[validate]}(:id)"), {"id": identifier})


def rejected(connection, identifier, *, validate=False):
    with pytest.raises(IntegrityError) as failure:
        call(connection, identifier, validate=validate)
        pytest.fail("The target historical response/receipt helper accepted the invalid candidate")
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"
    assert failure.value.orig.diag.message_primary == (
        "Credit revision receipt is inconsistent"
        if validate
        else "Credit revision response is inconsistent"
    )


def external(value):
    if isinstance(value, (UUID, date)):
        return str(value)
    return format(value, "f") if isinstance(value, Decimal) else value


def candidate(connection, state, sources, **kwargs):
    old = deepcopy(state["current"])
    previous = (
        stored(connection, Revision, state["previous"])
        if state["previous"]
        else (stored(connection, BusinessCredit, state["original"].id))
    )
    header, rows = transition(connection, state, sources, **kwargs)
    expected = dict(
        kind="business_credit_revision",
        credit_id=str(header["credit_id"]),
        revision_id=str(header["id"]),
        ledger_id=str(header["ledger_id"]),
        document_id=str(header["document_id"]),
        asset_id=state["original"].asset_id,
        previous_version=state["version"] - 1,
        version=state["version"],
        action=header["action"],
        reason=header["revision_reason"],
        previous_credit_date=str(previous["credit_date"]),
        previous_credit_reason=previous.get("credit_reason", previous.get("reason")),
        credit_date=external(header["credit_date"]),
        replacement_reason=header["credit_reason"],
        members=[],
    )
    for row in rows:
        saved = stored(connection, Member, row["id"])
        prior = old.get(row["source_line_id"])
        item = {
            key: external(saved[key])
            for key in (
                "action",
                "source_line_id",
                "source_operation_id",
                "operation_id",
                "operation_version",
                "reversal_journal_id",
                "line_no",
                "journal_id",
                "net_amount",
                "tax_amount",
                "total_amount",
                "recognition_date",
            )
        }
        for key in (
            "line_no",
            "operation_version",
            "journal_id",
            "net_amount",
            "tax_amount",
            "total_amount",
            "recognition_date",
        ):
            value = prior[key] if prior else None
            if value is not None and key.endswith("amount"):
                value = format(Decimal(value), ".18f")
            item["previous_" + key] = external(value)
        expected["members"].append(item)
    return header, rows, expected


def save_receipt(connection, header, expected, **changes):
    # Only a SQL shape fixture: raw request JSON/v2 hashing is not proved by these helpers.
    connection.execute(
        insert(CommandReceipt).values(
            **(
                dict(
                    ledger_id=header["ledger_id"],
                    key=header["idempotency_key"],
                    request_hash="a" * 64,
                    hash_version=2,
                    response_status=200,
                    operation_id=UUID(expected["members"][0]["operation_id"]),
                    response=expected,
                )
                | changes
            )
        )
    )


def content(connection):
    return {
        name: connection.execute(select(table).order_by(*table.primary_key.columns))
        .mappings()
        .all()
        for name, table in sorted(Base.metadata.tables.items())
    }


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("closed", [False, True])
def test_old_json_and_receipts_survive_later_versions_cancel_and_read_only_closure(
    drafts, kind, closed
):
    s = drafts
    state = group(s, kind=kind)
    a, b, c = state["source_ids"]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        original = connection.scalar(
            text("SELECT coinpup_business_credit_response(:id)"), {"id": state["original"].id}
        )
        versions = []
        for index, sources in enumerate(([a, c], [b, c], None)):

            def changes(row, step=index):
                return (
                    dict(net_amount=str(18 + step), tax_amount=str(2 - step))
                    if row["action"] != "cancel"
                    else {}
                )

            replacement = (
                dict(
                    credit_date=date(2026, 11, 4 + index),
                    credit_reason=f"Fictional replacement version {index + 2} 中文",
                )
                if sources
                else {}
            )
            header, _, expected = candidate(
                connection, state, sources, member_changes=changes, header_changes=replacement
            )
            save_receipt(connection, header, expected)
            versions.append((header, expected))
            unchanged = content(connection)
            for saved, body in versions:
                assert call(connection, saved["id"]) == body
                call(connection, saved["id"], validate=True)
            assert content(connection) == unchanged
            assert (
                connection.scalar(
                    text("SELECT coinpup_business_credit_response(:id)"),
                    {"id": state["original"].id},
                )
                == original
            )
        assert (
            versions[1][1]["version"] == 3
            and versions[1][1]["members"][0]["operation_version"] == 1
        )
        assert versions[-1][1]["credit_date"] is None
        assert versions[-1][1]["previous_credit_date"] == "2026-11-05"
        assert versions[-1][1]["previous_credit_reason"] == "Fictional replacement version 3 中文"
        assert versions[-1][1]["members"][0]["previous_net_amount"] == "19.000000000000000000"
        assert all(
            row["previous_total_amount"] == "20.000000000000000000" and row["total_amount"] is None
            for row in versions[-1][1]["members"]
        )
        if closed:
            # Close after all journals exist and validate the period state/audit/receipt chain.
            write_period(connection, s, new=date(2026, 11, 6))
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_ledger_periods_history, tr_ledger_period_audits_history, "
                "tr_ledger_period_receipts_history IMMEDIATE"
            )
            connection.execute(
                update(AssetRecord)
                .where(AssetRecord.asset_id == "USD")
                .values(
                    enabled=False,
                    version=AssetRecord.version + 1,
                )
            )
            connection.execute(
                update(Entity)
                .where(Entity.id == s["entity"].id)
                .values(
                    archived=True,
                    version=Entity.version + 1,
                )
            )
            unchanged = content(connection)
            for saved, body in versions:
                assert call(connection, saved["id"]) == body
                call(connection, saved["id"], validate=True)
            assert content(connection) == unchanged
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("selected,replace", [(200, False), (100, True)])
def test_full_actual_source_bound_keeps_stable_member_array_order(drafts, selected, replace):
    s = drafts
    state = group(s, count=200, selected=selected)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        sources = state["source_ids"][100:] if replace else list(reversed(state["source_ids"]))
        header, _, expected = candidate(connection, state, sources)
        save_receipt(connection, header, expected)
        assert len(expected["members"]) == 200
        assert call(connection, header["id"]) == expected
        call(connection, header["id"], validate=True)
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["missing", "v1", "201", "anchor", "other_revision"])
def test_receipt_metadata_is_checked_at_the_target_helper(drafts, fault):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        header, _, expected = candidate(connection, state, list(state["current"]))
        changes = (
            {"hash_version": 1}
            if fault == "v1"
            else {"response_status": 201}
            if fault == "201"
            else {}
        )
        if fault == "anchor":
            changes["operation_id"] = UUID(expected["members"][1]["operation_id"])
        if fault == "other_revision":
            _, _, other = candidate(connection, state, None)
            changes["response"] = other
        if fault != "missing":
            save_receipt(connection, header, expected, **changes)
        rejected(connection, header["id"], validate=True)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "kind",
        "credit_id",
        "revision_id",
        "ledger_id",
        "document_id",
        "asset_id",
        "previous_version",
        "version",
        "reason",
        "previous_credit_date",
        "previous_credit_reason",
        "credit_date",
        "replacement_reason",
        "missing",
        "extra",
        "order",
        "previous_net_amount",
        "previous_tax_amount",
        "previous_total_amount",
        "previous_recognition_date",
        "padding",
        "operation_version",
        "create_old",
        "cancel_old",
        "cancel_new",
        "decimal_version",
        "decimal_member_version",
        "decimal_line_no",
    ],
)
def test_entire_permanent_json_including_old_cancel_values_is_exact(drafts, fault):
    s = drafts
    state = group(s)
    a, _, c = state["source_ids"]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        candidate(connection, state, [a, c])
        header, _, expected = candidate(
            connection, state, None if fault.startswith("cancel_") else [a, state["source_ids"][1]]
        )
        body = deepcopy(expected)
        if fault in body:
            body[fault] = (
                body[fault] + 1 if isinstance(body[fault], int) else "Fictional altered value"
            )
        elif fault == "missing":
            body["members"].pop()
        elif fault == "extra":
            body["members"].append(deepcopy(body["members"][0]))
        elif fault == "order":
            body["members"].reverse()
        elif fault == "padding":
            body["members"][0]["previous_net_amount"] = "17.23"
        elif fault == "operation_version":
            next(row for row in body["members"] if row["action"] == "create")[fault] = body[
                "version"
            ]
        elif fault == "create_old":
            next(row for row in body["members"] if row["action"] == "create")[
                "previous_total_amount"
            ] = "20.000000000000000000"
        elif fault == "cancel_old":
            body["members"][0]["previous_total_amount"] = None
        elif fault == "cancel_new":
            body["members"][0]["total_amount"] = "0.000000000000000000"
        elif fault == "decimal_version":
            body["version"] = 3.0
        elif fault == "decimal_member_version":
            body["members"][0]["operation_version"] = 3.0
        elif fault == "decimal_line_no":
            body["members"][0]["line_no"] = 1.0
        else:
            body["members"][0][fault] = "Fictional altered value"
        save_receipt(connection, header, body)
        rejected(connection, header["id"], validate=True)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["earlier", "other_operation", "duplicate", "predecessor"])
def test_response_requires_unique_immediately_previous_fact_including_cross_group_aliases(
    drafts, fault
):
    s = drafts
    state = group(s, selected=1 if fault == "duplicate" else 2)
    original = state["original"]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        other = pending_origin(connection, original) if fault == "duplicate" else None
        transition(connection, state, list(state["current"]))
        changes = {}
        if fault in ("earlier", "other_operation"):
            changes["member_changes"] = lambda row: {
                "previous_journal_id": original.postings[0 if fault == "earlier" else 1].journal_id
            }
        if fault == "predecessor":
            changes["header_changes"] = {"group_version": 4}
        header, _ = transition(connection, state, list(state["current"]), **changes)
        if fault == "duplicate":
            # Filtering facts by credit would hide this same-ledger other group's alias.
            duplicate = header | dict(
                id=uuid4(),
                credit_id=other,
                group_version=2,
                previous_revision_id=None,
                idempotency_key=str(uuid4()),
            )
            connection.execute(insert(Revision).values(**duplicate))
            alias = member(duplicate, original, "create")
            connection.execute(insert(Member).values(**alias))
            header = stored(connection, Revision, header["previous_revision_id"])
        rejected(connection, header["id"])
        transaction.rollback()
    assert snapshot(s) == before


def test_appending_to_saved_historical_members_breaks_its_permanent_receipt(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        header, _, expected = candidate(connection, state, list(state["current"]))
        save_receipt(connection, header, expected)
        candidate(connection, state, None)
        alias = member(header, state["original"], "create")
        operation, journal = uuid4(), uuid4()
        born = stored(connection, FinancialOperation, state["original"].postings[0].operation_id)
        connection.execute(
            insert(FinancialOperation).values(
                **(
                    born
                    | dict(
                        id=operation,
                        version=1,
                        status="active",
                        current_journal_id=journal,
                    )
                )
            )
        )
        copy_posting(
            connection,
            state["original"].postings[0].journal_id,
            dict(
                id=journal,
                operation_id=operation,
                transaction_date=header["credit_date"],
                recognition_date=alias["recognition_date"],
                description=header["credit_reason"],
            ),
            line_changes={"document_line_id": state["source_ids"][2]},
        )
        alias.update(
            operation_id=operation,
            journal_id=journal,
            net_amount="17.23",
            tax_amount="2.77",
            total_amount="20",
            member_no=3,
            line_no=3,
            source_line_id=state["source_ids"][2],
            source_operation_id=state["source_operations"][state["source_ids"][2]],
        )
        connection.execute(insert(Member).values(**alias))
        rejected(connection, header["id"], validate=True)
        transaction.rollback()
    assert snapshot(s) == before


def test_unknown_identifiers_and_empty_roundtrip_preserve_old_functions_and_original_history(
    drafts,
):
    s = drafts
    group(s)
    before = snapshot(s)
    for validate in (False, True):
        with s["engine"].connect() as connection:
            transaction = connection.begin()
            rejected(connection, uuid4(), validate=validate)
            transaction.rollback()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        functions, triggers = definitions(connection)
        migration = credit_receipt_migration()
        migration["downgrade"]()
        assert definitions(connection) == (
            [
                body
                for body in functions
                if not any(name in body.split("\n", 1)[0] for name in FUNCTIONS)
            ],
            triggers,
        )
        migration["upgrade"]()
        assert definitions(connection) == (functions, triggers)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "history",
    ["rows", "business_credit_revisions", "business_credit_revision_members", "business_credits"],
)
def test_any_revision_or_notification_history_refuses_helper_downgrade(drafts, history):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        if history == "rows":
            transition(connection, state, None)
        else:
            connection.exec_driver_sql(
                f"CREATE TEMP TABLE {history}(id uuid,ledger_id uuid,version int) ON COMMIT DROP; "
                "CREATE TRIGGER fictional_receipt_history AFTER INSERT "
                f"ON pg_temp.{history} FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
            )
            connection.execute(
                text(f"INSERT INTO pg_temp.{history} VALUES (:id,:ledger,:version)"),
                {
                    "id": uuid4(),
                    "ledger": s["ledger"],
                    "version": 2 if history == "business_credits" else 1,
                },
            )
        with Operations.context(MigrationContext.configure(connection)):
            credit_receipt_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
