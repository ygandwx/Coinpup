"""Fictional immutable storage probes keep unvalidated returns from committing."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn,
    BusinessAllocationReturnLine,
)
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.allocation_fixture import source, write
from tests.integration.business.payment_fixture import source as payment_source
from tests.integration.business.payment_fixture import write as payment_write
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import (
    allocation_return_storage_migration,
    allocation_return_validation_migration,
)
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
TABLES = (BusinessAllocationReturn, BusinessAllocationReturnLine)


def rows(
    s,
    *,
    direction="payout",
    payment_asset="USD",
    document_asset="USD",
    source_payment_amount="110",
    source_document_amount="110",
    return_payment_amount="20",
    return_document_amount="20",
):
    facts = source(s, direction, payment_asset=payment_asset, document_asset=document_asset)
    with s["engine"].begin() as c:
        result = write(
            c,
            s,
            *facts,
            payment_amount=source_payment_amount,
            document_amount=source_document_amount,
        )
        original = (
            c.execute(
                select(BusinessAllocationLine).where(
                    BusinessAllocationLine.allocation_id == result["id"]
                )
            )
            .mappings()
            .one()
        )
    group = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        payment_id=facts[0].id,
        return_date=date(2026, 10, 1),
        reason="Fictional return storage 中文",
        created_by=s["owner"],
        idempotency_key="fictional-allocation-return-" + result["id"],
    )
    operation_id, journal_id = uuid4(), uuid4()
    member = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        return_id=group["id"],
        payment_id=facts[0].id,
        source_allocation_line_id=original["id"],
        source_version=1,
        source_return_no=1,
        line_no=1,
        payment_amount=Decimal(return_payment_amount),
        document_amount=Decimal(return_document_amount),
        operation_id=operation_id,
        journal_id=journal_id,
    )
    payment, advance, document, control = facts
    paid = Amount.parse(return_payment_amount, ASSETS[payment.asset_id])
    applied = Amount.parse(return_document_amount, ASSETS[document.asset_id])
    bank_sign = 1 if direction == "receipt" else -1
    dimensions = dict(
        party_id=payment.party_id,
        document_id=document.id,
        document_line_id=document.lines[0].id,
        project_id=document.lines[0].project_id,
    )
    principal = dict(party_id=payment.party_id)
    journal_rows = [
        PostingLine(
            journal_id=journal_id,
            ledger_id=s["ledger"],
            line_no=1,
            role="account",
            asset_id=payment.asset_id,
            amount=Amount(paid.asset, -bank_sign * paid.minor_units),
            account_id=advance,
            **principal,
        ),
        PostingLine(
            journal_id=journal_id,
            ledger_id=s["ledger"],
            line_no=2,
            role="account",
            asset_id=document.asset_id,
            amount=Amount(applied.asset, bank_sign * applied.minor_units),
            account_id=control,
            **dimensions,
        ),
    ]
    if payment.asset_id != document.asset_id:
        journal_rows.extend(
            [
                PostingLine(
                    journal_id=journal_id,
                    ledger_id=s["ledger"],
                    line_no=3,
                    role="exchange",
                    asset_id=payment.asset_id,
                    amount=Amount(paid.asset, bank_sign * paid.minor_units),
                    **principal,
                ),
                PostingLine(
                    journal_id=journal_id,
                    ledger_id=s["ledger"],
                    line_no=4,
                    role="exchange",
                    asset_id=document.asset_id,
                    amount=Amount(applied.asset, -bank_sign * applied.minor_units),
                    **dimensions,
                ),
            ]
        )
    return {
        "facts": facts,
        "group": group,
        "member": member,
        "operation": {
            "id": operation_id,
            "ledger_id": s["ledger"],
            "kind": "unallocation",
            "current_journal_id": journal_id,
            "created_by": s["owner"],
        },
        "journal": {
            "id": journal_id,
            "operation_id": operation_id,
            "ledger_id": s["ledger"],
            "transaction_date": group["return_date"],
            "recognition_date": group["return_date"],
            "description": group["reason"],
        },
        "journal_lines": prepare_journal_lines(journal_rows, ASSETS),
        "source_operation_id": original["operation_id"],
        "source_journal_id": original["journal_id"],
    }


def pending(c, values, *, include_line=True, legacy=False):
    c.exec_driver_sql("SELECT pg_advisory_xact_lock(18945999704708432)")
    c.execute(insert(TABLES[0]).values(**values["group"]))
    if not legacy:
        c.execute(insert(FinancialOperation).values(**values["operation"]))
        c.execute(insert(Journal).values(**values["journal"]))
        c.execute(insert(JournalLine), values["journal_lines"])
    member = values["member"]
    if legacy:
        member = member | {
            "operation_id": values["source_operation_id"],
            "journal_id": values["source_journal_id"],
        }
    if include_line:
        c.execute(insert(TABLES[1]).values(**member))
    response = c.scalar(
        select(func.coinpup_business_allocation_return_response(values["group"]["id"]))
    )
    c.execute(
        insert(CommandReceipt).values(
            ledger_id=values["group"]["ledger_id"],
            key=values["group"]["idempotency_key"],
            request_hash="a" * 64,
            hash_version=2,
            response=response,
            response_status=201,
            operation_id=member["operation_id"],
        )
    )


def another_return(values, *, payment_amount, document_amount, source_return_no):
    group_id, operation_id, journal_id = uuid4(), uuid4(), uuid4()
    group = values["group"] | {
        "id": group_id,
        "reason": "Fictional residual return 中文",
        "idempotency_key": "fictional-allocation-return-" + str(group_id),
    }
    member = values["member"] | {
        "id": uuid4(),
        "return_id": group_id,
        "source_return_no": source_return_no,
        "payment_amount": Decimal(payment_amount),
        "document_amount": Decimal(document_amount),
        "operation_id": operation_id,
        "journal_id": journal_id,
    }
    payment = values["facts"][0]
    bank_sign = 1 if payment.direction == "receipt" else -1
    paid = Amount.parse(payment_amount, ASSETS[payment.asset_id])
    applied = Amount.parse(document_amount, ASSETS[values["facts"][2].asset_id])
    journal_lines = []
    for row in values["journal_lines"]:
        if row["asset_id"] == payment.asset_id:
            sign = -bank_sign if row["role"] == "account" else bank_sign
            quantity = Amount(paid.asset, sign * paid.minor_units)
        else:
            sign = bank_sign if row["role"] == "account" else -bank_sign
            quantity = Amount(applied.asset, sign * applied.minor_units)
        journal_lines.append(
            row | {"id": uuid4(), "journal_id": journal_id, "amount": quantity.to_decimal()}
        )
    return {
        **values,
        "group": group,
        "member": member,
        "operation": values["operation"] | {"id": operation_id, "current_journal_id": journal_id},
        "journal": values["journal"]
        | {"id": journal_id, "operation_id": operation_id, "description": group["reason"]},
        "journal_lines": journal_lines,
    }


def test_return_notifications_are_atomic_and_original_mapping_is_not_overwritten(drafts):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        transaction = c.begin()
        try:
            pending(c, values)
            assert set(
                c.execute(
                    select(ChangeLog.entity_type, ChangeLog.entity_id).where(
                        ChangeLog.entity_type.in_([table.__tablename__ for table in TABLES])
                    )
                ).all()
            ) == {
                (table.__tablename__, str(row["id"]))
                for table, row in zip(TABLES, (values["group"], values["member"]), strict=True)
            }
            assert c.scalar(select(BusinessAllocationReturnLine.payment_amount)) == Decimal("20")
        finally:
            transaction.rollback()
    assert snapshot(s) == before


def test_valid_same_asset_return_commits_with_only_two_balanced_lines(drafts):
    values = rows(drafts)
    with drafts["engine"].begin() as c:
        pending(c, values)
    with drafts["engine"].connect() as c:
        assert (
            c.scalar(
                select(func.count())
                .select_from(JournalLine)
                .where(JournalLine.journal_id == values["member"]["journal_id"])
            )
            == 2
        )


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_valid_cross_asset_return_releases_only_its_net_capacity(drafts, direction):
    s = drafts
    values = rows(
        s,
        direction=direction,
        payment_asset="USD",
        document_asset="EUR",
        source_payment_amount="110",
        source_document_amount="100",
        return_payment_amount="22",
        return_document_amount="20",
    )
    with s["engine"].begin() as c:
        pending(c, values)
    residual = another_return(values, payment_amount="88", document_amount="80", source_return_no=2)
    with s["engine"].begin() as c:
        pending(c, residual)
    with s["engine"].begin() as c:
        write(
            c,
            s,
            *values["facts"],
            payment_amount="22",
            document_amount="20",
            allocation_date="2026-10-01",
        )
    with s["engine"].connect() as c:
        assert c.scalar(
            select(func.sum(BusinessAllocationLine.payment_amount)).where(
                BusinessAllocationLine.payment_id == values["group"]["payment_id"]
            )
        ) == Decimal("132")
        assert c.scalar(
            select(func.sum(BusinessAllocationReturnLine.payment_amount)).where(
                BusinessAllocationReturnLine.payment_id == values["group"]["payment_id"]
            )
        ) == Decimal("110")
        assert c.scalar(
            select(func.sum(BusinessAllocationLine.document_amount)).where(
                BusinessAllocationLine.document_line_id == values["facts"][2].lines[0].id
            )
        ) == Decimal("120")
        assert c.scalar(
            select(func.sum(BusinessAllocationReturnLine.document_amount)).where(
                BusinessAllocationReturnLine.payment_id == values["group"]["payment_id"]
            )
        ) == Decimal("100")


def test_cross_asset_return_rejects_a_value_outside_the_original_ratio(drafts):
    s = drafts
    values = rows(
        s,
        payment_asset="USD",
        document_asset="EUR",
        source_payment_amount="110",
        source_document_amount="100",
        return_payment_amount="21.99",
        return_document_amount="20",
    )
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values)
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_return_shape"


@pytest.mark.parametrize("include_line", [False, True])
def test_return_cannot_commit_with_empty_or_legacy_financial_members(drafts, include_line):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values, include_line=include_line, legacy=include_line)
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_return_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("action", ["update", "delete"])
def test_uncommitted_return_history_cannot_be_rewritten(drafts, table, action):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values)
        c.execute(delete(table) if action == "delete" else update(table).values(version=2))
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_return_immutable"
    assert snapshot(s) == before


def test_two_groups_cannot_claim_the_same_original_return_position(drafts):
    s = drafts
    values = rows(s)
    group, member = values["group"], values["member"]
    another_group = group | {
        "id": uuid4(),
        "idempotency_key": "fictional-return-duplicate-" + str(group["payment_id"]),
    }
    another_operation, another_journal = uuid4(), uuid4()
    another_member = member | {
        "id": uuid4(),
        "return_id": another_group["id"],
        "operation_id": another_operation,
        "journal_id": another_journal,
    }
    another = {
        **values,
        "group": another_group,
        "member": another_member,
        "operation": values["operation"]
        | {"id": another_operation, "current_journal_id": another_journal},
        "journal": values["journal"] | {"id": another_journal, "operation_id": another_operation},
        "journal_lines": [
            line | {"id": uuid4(), "journal_id": another_journal}
            for line in values["journal_lines"]
        ],
    }
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values)
        pending(c, another)
    assert (
        error.value.orig.diag.constraint_name == "uq_business_allocation_return_line_source_order"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("source_allocation_line_id", "foreign", "fk_business_allocation_return_line_source"),
        ("payment_id", "foreign", "fk_business_allocation_return_line_group"),
        ("line_no", 0, "ck_business_allocation_return_line_position"),
        ("line_no", 201, "ck_business_allocation_return_line_position"),
        ("source_version", 0, "ck_business_allocation_return_line_source_version"),
        ("source_return_no", 0, "ck_business_allocation_return_line_source_order"),
        ("payment_amount", Decimal("0"), "ck_business_allocation_return_line_amounts"),
        ("document_amount", Decimal("-1"), "ck_business_allocation_return_line_amounts"),
    ],
)
def test_original_scope_and_quantity_representation_are_enforced(drafts, field, value, constraint):
    s = drafts
    values = rows(s)
    member = values["member"]
    member[field] = uuid4() if value == "foreign" else value
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values)
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize("foreign_reference", ["payment", "original_mapping"])
def test_valid_references_in_another_owned_ledger_cannot_cross_the_return_scope(
    drafts, foreign_reference
):
    s = drafts
    values = rows(s)
    group, member = values["group"], values["member"]
    entity = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other return ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    account = s["structure"].create_account(
        s["owner"],
        entity.ledger.id,
        AccountCreate(name="Fictional other bank", kind="bank", asset_ids=["USD"]),
    )
    categories = s["structure"].list_categories(s["owner"], entity.ledger.id)
    other = s | {
        "ledger": entity.ledger.id,
        "account": account,
        "expenses": [item for item in categories if item.kind == "expense"],
    }
    if foreign_reference == "original_mapping":
        foreign_facts = source(other)
        with s["engine"].begin() as c:
            foreign_allocation = write(
                c,
                other,
                *foreign_facts,
                payment_amount="110",
                document_amount="110",
            )
        with s["engine"].connect() as c:
            member["source_allocation_line_id"] = c.scalar(
                select(BusinessAllocationLine.id).where(
                    BusinessAllocationLine.allocation_id == UUID(foreign_allocation["id"])
                )
            )
        constraint = "fk_business_allocation_return_line_source"
    else:
        payment, advance = payment_source(other, fee_count=0)
        with s["engine"].begin() as c:
            payment_write(c, other, payment, advance)
        group["payment_id"] = payment.id
        constraint = "fk_business_allocation_return_payment"
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values)
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def test_empty_round_trip_restores_guards_and_historical_notifications_block_downgrade(drafts):
    s = drafts
    migration = allocation_return_storage_migration()
    validation = allocation_return_validation_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        validation["downgrade"]()
        migration["downgrade"]()
        migration["upgrade"]()
        validation["upgrade"]()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        # Record isolated fictional history through the real trigger; no guard is disabled.
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_allocation_returns(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; CREATE TRIGGER fictional_return_history AFTER INSERT ON "
            "pg_temp.business_allocation_returns FOR EACH ROW EXECUTE FUNCTION "
            "public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_allocation_returns VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(c)):
            validation["downgrade"]()
    assert (
        error.value.orig.diag.constraint_name
        == "ck_business_allocation_return_validation_downgrade"
    )
    assert snapshot(s) == before
