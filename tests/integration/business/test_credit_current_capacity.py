"""Current credit capacity is consistent, while complete revision commits remain gated."""

from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.business.settlement_totals import document_totals
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_credit_fact_validation import leaf as posting_fact
from tests.integration.business.test_credit_projections import gated
from tests.integration.business.test_credit_revision_receipts import candidate, save_receipt
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay
from tests.integration.conftest import credit_current_capacity_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def other_credit(s, state, source, net="0.01", tax="0"):
    original = state["original"]
    request = BusinessCreditCreate(
        id=uuid4(),
        document_id=original.document_id,
        expected_version=original.source_version,
        credit_date=original.credit_date,
        reason="Fictional independent capacity group",
        lines=[
            dict(
                source_line_id=source,
                net_amount=net,
                tax_amount=tax,
                recognition_date=original.credit_date,
            )
        ],
    )
    return credit(s, request, key="fictional-other-capacity"), request


def complete_candidate(connection, state, sources, **kwargs):
    header, members, response = candidate(connection, state, sources, **kwargs)
    save_receipt(connection, header, response)
    for row in members:
        if row["journal_id"]:
            posting_fact(connection, row["journal_id"])
    connection.execute(
        text("SELECT coinpup_validate_credit_group_transitions(:id)"), {"id": state["original"].id}
    )
    connection.execute(
        text("SELECT coinpup_validate_credit_revision_receipt(:id)"), {"id": header["id"]}
    )


def validate(connection, kind, identifier, *, exceeds=False):
    query = text(f"SELECT coinpup_validate_business_{kind}(:id)")
    if not exceeds:
        connection.execute(query, {"id": identifier})
        return
    # Keep the outer candidate available for the other target and explicit closed-gate probe.
    with pytest.raises(IntegrityError) as failure, connection.begin_nested():
        connection.execute(query, {"id": identifier})
        pytest.fail("The capacity validator accepted an over-capacity current projection")
    assert failure.value.orig.diag.constraint_name == f"ck_business_{kind}_shape"
    assert failure.value.orig.diag.message_primary == (
        "Business credit line shape is invalid"
        if kind == "credit"
        else "Allocation line shape is invalid"
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_current_totals_follow_remove_rejoin_and_cancel_without_recounting_history(drafts, kind):
    s = drafts
    state = group(s, kind=kind)
    a, b, c = state["source_ids"]
    other, request = other_credit(s, state, a)
    before = snapshot(s)
    with s["engine"].connect() as connection, localcontext() as context:
        transaction = connection.begin()
        # All arithmetic expectations are literal strings, independent of Decimal context.
        context.prec = 1
        for selected, expected in (
            ([a, c], {a: ("17.24", "2.77"), c: ("17.23", "2.77")}),
            ([b, c], {a: ("0.01", "0"), b: ("17.23", "2.77"), c: ("17.23", "2.77")}),
            (None, {a: ("0.01", "0")}),
        ):
            complete_candidate(connection, state, selected)
            assert document_totals(connection, s["ledger"], [a, b, c]) == {
                source: (Decimal("0"), Decimal(net), Decimal(tax))
                for source, (net, tax) in expected.items()
            }
            assert document_totals(connection, uuid4(), [a, b, c]) == {}
            validate(connection, "credit", other.id)
        gated(connection)
        transaction.rollback()
    assert credit(s, request, key="fictional-other-capacity") == other
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("component", ["net", "tax"])
@pytest.mark.parametrize("exceeds", [False, True])
def test_credit_capacity_checks_current_net_and_tax_against_another_valid_group(
    drafts, kind, component, exceeds
):
    s = drafts
    state = group(s, kind=kind, count=1, selected=1)
    source = state["source_ids"][0]
    other, request = other_credit(
        s,
        state,
        source,
        net="82.77" if component == "net" else "0",
        tax="7.23" if component == "tax" else "0",
    )
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        net, tax = ("17.24", "2.76") if component == "net" else ("17.22", "2.78")
        changes = dict(net_amount=net, tax_amount=tax) if exceeds else {}
        # Gross remains 20: copied real journal lines remain exact; only hand-entered split changes.
        complete_candidate(connection, state, [source], member_changes=lambda row: changes)
        assert document_totals(connection, s["ledger"], [source])[source][1:] == (
            (Decimal("100.01"), Decimal("2.76"))
            if component == "net" and exceeds
            else (Decimal("100"), Decimal("2.77"))
            if component == "net"
            else (Decimal("17.22"), Decimal("10.01"))
            if exceeds
            else (Decimal("17.23"), Decimal("10"))
        )
        # This group itself remains active/v1; rejection must reach the capacity comparison.
        validate(connection, "credit", other.id, exceeds=exceeds)
        gated(connection)
        transaction.rollback()
    assert credit(s, request, key="fictional-other-capacity") == other
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("applied,exceeds", [("80", False), ("80.01", True)])
def test_allocation_and_credit_sql_share_current_gross_capacity(drafts, kind, applied, exceeds):
    s = drafts
    state = group(s, kind=kind)
    original = state["original"]
    a, _, c = state["source_ids"]
    other, request = other_credit(s, state, c, net="10")
    if kind == "bill":
        s["posting"].post_opening(
            s["owner"], s["ledger"], opening(s, "1000", "USD"), "fictional-capacity-opening"
        )
    payload = BusinessPaymentCreate(
        id=uuid4(),
        direction="receipt" if kind == "invoice" else "payout",
        party_id=s["party"].id,
        account_id=s["account"].id,
        asset_id="USD",
        amount="100",
        transaction_date=original.credit_date,
        description="Fictional current capacity payment",
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
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        complete_candidate(connection, state, [a, c])
        assert document_totals(connection, s["ledger"], [c]) == {
            c: (Decimal(applied), Decimal("27.23"), Decimal("2.77"))
        }
        validate(connection, "allocation", paid.initial_allocation.id, exceeds=exceeds)
        validate(connection, "credit", other.id, exceeds=exceeds)
        gated(connection)
        transaction.rollback()
    assert pay(s, payload) == paid
    assert credit(s, request, key="fictional-other-capacity") == other
    assert snapshot(s) == before


def test_current_capacity_empty_roundtrip_restores_exact_frozen_validators_and_receipts(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    migration = credit_current_capacity_migration()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        current = definitions(connection)
        migration["downgrade"]()
        for key, name in (("_OLD_ALLOCATION", "allocation"), ("_OLD_CREDIT", "credit")):
            assert (
                connection.scalar(
                    text(
                        "SELECT prosrc FROM pg_proc WHERE oid="
                        f"'coinpup_validate_business_{name}(uuid)'::regprocedure"
                    )
                )
                == (migration[key].split("AS $$", 1)[1].rsplit("$$", 1)[0])
            )
        validate(connection, "credit", state["original"].id)
        migration["upgrade"]()
        assert definitions(connection) == current
        validate(connection, "credit", state["original"].id)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "history",
    ["rows", "business_credit_revisions", "business_credit_revision_members", "business_credits"],
)
def test_current_capacity_downgrade_refuses_revision_or_notification_history(drafts, history):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        if history == "rows":
            complete_candidate(connection, state, None)
        else:
            connection.exec_driver_sql(
                f"CREATE TEMP TABLE {history}(id uuid,ledger_id uuid,version int) ON COMMIT DROP; "
                f"CREATE TRIGGER fictional_capacity_history AFTER INSERT ON pg_temp.{history} "
                "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
            )
            connection.execute(
                text(f"INSERT INTO pg_temp.{history} VALUES (:id,:ledger,:version)"),
                dict(
                    id=uuid4(),
                    ledger=s["ledger"],
                    version=2 if history == "business_credits" else 1,
                ),
            )
        with Operations.context(MigrationContext.configure(connection)):
            credit_current_capacity_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
