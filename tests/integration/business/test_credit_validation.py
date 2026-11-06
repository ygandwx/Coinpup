"""Direct fictional credit writes prove shape and capacity independently of application plans."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.credit_models import BusinessCredit, BusinessCreditLine
from coinpup_api.business.credit_receipts import validate_credit_receipt_amounts
from coinpup_api.business.credit_schemas import BusinessCreditResponse
from coinpup_api.business.settlement_balances import BusinessSettlementBalances
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate, BusinessPaymentCreate
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.common import PostingCore
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import submit_return
from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import allocate, pay
from tests.integration.conftest import credit_validation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def source(s, kind="invoice"):
    document, command = prepare(s, kind=kind)
    return document, submit(s, document, command)


def write(
    s,
    original,
    *,
    net="17.23",
    tax="2.77",
    header_changes=None,
    line_changes=None,
    journal_changes=None,
    row_changes=None,
    receipt_changes=None,
):
    document, confirmation = original
    line, posting = document.lines[0], confirmation.postings[0]
    invoice = document.document_kind == "invoice"
    key = "receivable.customer" if invoice else "payable.supplier"
    category = "income" if invoice else "expense"
    net_amount, tax_amount = Amount.parse(net, ASSETS["USD"]), Amount.parse(tax, ASSETS["USD"])
    total = net_amount + tax_amount
    category_amount = total if invoice else -total
    credit_id, operation_id, journal_id = uuid4(), uuid4(), uuid4()
    with Session(s["engine"]) as session, session.begin():
        session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        acquire_write_lock(session)
        PostingCore._locked_ledger(session, s["owner"], s["ledger"])
        controls = ControlAccounts(s["engine"]).ensure(
            session, s["owner"], s["ledger"], {key: {"USD"}}, lock_assets={"USD"}
        )
        parameters = prepare_journal_lines(
            [
                PostingLine(
                    journal_id=journal_id,
                    ledger_id=s["ledger"],
                    line_no=1,
                    role="account",
                    account_id=controls[key].id,
                    asset_id="USD",
                    amount=-category_amount,
                    party_id=document.party_id,
                    document_id=document.id,
                    document_line_id=line.id,
                    project_id=line.project_id,
                ),
                PostingLine(
                    journal_id=journal_id,
                    ledger_id=s["ledger"],
                    line_no=2,
                    role=category,
                    category_id=line.category_id,
                    asset_id="USD",
                    amount=category_amount,
                    party_id=document.party_id,
                    document_id=document.id,
                    document_line_id=line.id,
                    project_id=line.project_id,
                ),
            ],
            ASSETS,
        )
        if row_changes:
            parameters = [row | row_changes for row in parameters]
        now = session.scalar(select(func.clock_timestamp()))
        idempotency_key = str(uuid4())
        header = dict(
            id=credit_id,
            ledger_id=s["ledger"],
            document_id=document.id,
            source_version=confirmation.document_version,
            credit_date=date(2026, 10, 21),
            reason="Fictional manual credit 虚构贷项",
            created_by=s["owner"],
            idempotency_key=idempotency_key,
            created_at=now,
        ) | (header_changes or {})
        session.execute(insert(BusinessCredit).values(**header))
        session.execute(
            insert(FinancialOperation).values(
                id=operation_id,
                ledger_id=s["ledger"],
                kind="invoice_credit" if invoice else "bill_credit",
                current_journal_id=journal_id,
                created_by=s["owner"],
                created_at=now,
                updated_at=now,
            )
        )
        session.execute(
            insert(Journal).values(
                **(
                    dict(
                        id=journal_id,
                        operation_id=operation_id,
                        ledger_id=s["ledger"],
                        transaction_date=header["credit_date"],
                        recognition_date=date(2026, 10, 20),
                        description=header["reason"],
                        created_at=now,
                    )
                    | (journal_changes or {})
                )
            )
        )
        session.execute(insert(JournalLine), parameters)
        session.execute(
            insert(BusinessCreditLine).values(
                **(
                    dict(
                        credit_id=credit_id,
                        document_id=document.id,
                        ledger_id=s["ledger"],
                        source_line_id=line.id,
                        source_operation_id=posting.operation_id,
                        line_no=1,
                        net_amount=net_amount.to_decimal(),
                        tax_amount=tax_amount.to_decimal(),
                        total_amount=total.to_decimal(),
                        recognition_date=date(2026, 10, 20),
                        operation_id=operation_id,
                        journal_id=journal_id,
                        created_at=now,
                    )
                    | (line_changes or {})
                )
            )
        )
        response = session.scalar(
            text("SELECT coinpup_business_credit_response(:id)"), {"id": credit_id}
        )
        session.execute(
            insert(CommandReceipt).values(
                ledger_id=s["ledger"],
                key=idempotency_key,
                hash_version=2,
                request_hash="c" * 64,
                operation_id=operation_id,
                response_status=201,
                response=response | (receipt_changes or {}),
                created_at=now,
            )
        )
    return validate_credit_receipt_amounts(
        BusinessCreditResponse.model_validate(response), ASSETS["USD"]
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("net,tax", [("17.23", "2.77"), ("0.01", "0"), ("0", "0.01")])
def test_valid_manual_credit_commits_without_changing_cash_or_original_receipt(
    drafts, kind, net, tax
):
    s = drafts
    original = source(s, kind)
    result = write(s, original, net=net, tax=tax)
    assert result.postings[0].source_operation_id == original[1].postings[0].operation_id
    with s["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(JournalLine).where(JournalLine.journal_id == result.postings[0].journal_id)
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2 and not any(row["account_id"] == s["account"].id for row in rows)
        units = [Amount.from_decimal(row["amount"], ASSETS["USD"]).minor_units for row in rows]
        assert sum(units) == 0
        assert connection.scalar(
            select(Journal.sealed).where(Journal.id == result.postings[0].journal_id)
        )
    current = BusinessSettlementBalances(s["engine"]).document_capacity(
        s["owner"], s["ledger"], original[0].id
    )
    credited = Amount.parse(net, ASSETS["USD"]) + Amount.parse(tax, ASSETS["USD"])
    assert current.total_amount == "110.00" and current.applied_amount == "0.00"
    assert current.outstanding_amount == (Amount.parse("110", ASSETS["USD"]) - credited).to_string()
    before = snapshot(s)
    assert (
        submit(
            s,
            original[0],
            BusinessConfirmationCreate(
                id=original[1].id, expected_version=original[1].source_version
            ),
        )
        == original[1]
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"net": "100.01", "tax": "0"},
        {"net": "0", "tax": "10.01"},
        {"header_changes": {"source_version": 1}},
        {"header_changes": {"credit_date": date(1, 1, 1)}},
        {"journal_changes": {"description": "Fictional mismatched reason"}},
        {"journal_changes": {"recognition_date": date(2026, 10, 19)}},
        {"line_changes": {"net_amount": Decimal("19.999"), "tax_amount": Decimal("0.001")}},
        {"line_changes": {"source_operation_id": uuid4()}},
        {"row_changes": {"project_id": None}},
        {"receipt_changes": {"postings": []}},
        {"receipt_changes": {"source_version": 1}},
    ],
)
def test_direct_database_credit_cannot_bypass_source_shape_or_complete_receipt(drafts, changes):
    s = drafts
    original = source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        write(s, original, **changes)
    assert snapshot(s) == before


def test_concurrent_credits_cannot_consume_the_same_original_net_capacity(drafts):
    s = drafts
    original = source(s)

    def attempt(_):
        try:
            return write(s, original, net="60", tax="0")
        except IntegrityError as error:
            assert error.orig.diag.constraint_name == "ck_business_credit_shape"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(row is not None for row in results) == 1
    with s["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(BusinessCredit)) == 1


def test_credit_and_allocation_cannot_concurrently_consume_the_same_capacity(drafts):
    s = drafts
    original = source(s)
    document = original[0]
    payment = BusinessPaymentCreate(
        id=uuid4(),
        direction="receipt",
        party_id=document.party_id,
        account_id=s["account"].id,
        asset_id="USD",
        amount="110",
        transaction_date=document.issue_date,
        description="Fictional concurrent capacity",
    )
    paid = pay(s, payment)
    group = BusinessAllocationCreate(
        id=uuid4(),
        payment_id=payment.id,
        allocation_date=document.issue_date,
        lines=[
            dict(
                document_id=document.id,
                document_line_id=document.lines[0].id,
                payment_amount="60",
                document_amount="60",
            )
        ],
    )
    barrier = Barrier(2)

    def attempt(credit):
        barrier.wait(timeout=15)
        try:
            return write(s, original, net="60", tax="0") if credit else allocate(s, group)
        except IntegrityError as error:
            assert credit and error.orig.diag.constraint_name == "ck_business_credit_shape"
        except LedgerError as error:
            assert not credit and error.code == "allocation_exceeds_document"
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [True, False]))
    assert sum(row is not None for row in results) == 1
    current = BusinessSettlementBalances(s["engine"]).document_capacity(
        s["owner"], s["ledger"], document.id
    )
    assert current.outstanding_amount == "50.00"
    assert current.applied_amount == ("0.00" if results[0] is not None else "60.00")
    before = snapshot(s)
    assert pay(s, payment) == paid
    assert snapshot(s) == before


def test_credit_requires_explicit_return_of_already_settled_capacity(drafts):
    s = drafts
    original = source(s)
    document = original[0]
    payment = BusinessPaymentCreate(
        id=uuid4(),
        direction="receipt",
        party_id=document.party_id,
        account_id=s["account"].id,
        asset_id="USD",
        amount="110",
        transaction_date=document.issue_date,
        description="Fictional credit capacity settlement",
        initial_allocation=dict(
            id=uuid4(),
            allocation_date=document.issue_date,
            lines=[
                dict(
                    document_id=document.id,
                    document_line_id=document.lines[0].id,
                    payment_amount="110",
                    document_amount="110",
                )
            ],
        ),
    )
    paid = pay(s, payment)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        write(s, original, net="0.01", tax="0")
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
    assert snapshot(s) == before
    with s["engine"].connect() as connection:
        identifier = connection.scalar(
            select(BusinessAllocationLine.id).where(
                BusinessAllocationLine.allocation_id == payment.initial_allocation.id
            )
        )
    request = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=payment.id,
        return_date="2026-10-20",
        reason="Fictional explicit release before credit",
        lines=[
            dict(
                source_allocation_line_id=identifier,
                expected_version=1,
                payment_amount="20",
                document_amount="20",
            )
        ],
    )
    returned = submit_return(s, request)
    write(s, original, net="17.23", tax="2.77")
    current = BusinessSettlementBalances(s["engine"]).document_capacity(
        s["owner"], s["ledger"], document.id
    )
    assert current.applied_amount == "90.00" and current.outstanding_amount == "0.00"
    before = snapshot(s)
    assert pay(s, payment) == paid
    assert submit_return(s, request) == returned
    assert snapshot(s) == before


def test_credit_history_blocks_downgrade_and_empty_history_restores_original_dispatch(drafts):
    s = drafts
    migration = credit_validation_migration()
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        migration["downgrade"]()
        restored = connection.scalar(
            text("SELECT pg_get_functiondef('coinpup_validate_posting_shape(uuid)'::regprocedure)")
        )
        assert "coinpup_validate_shape_credit" not in restored
        assert "coinpup_validate_shape_refund" in restored
        migration["upgrade"]()
    write(s, source(s))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_credit_downgrade"
    assert snapshot(s) == before


def test_credit_validation_downgrade_rejects_notification_only_history(drafts):
    s = drafts
    migration = credit_validation_migration()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.business_credits")) == 0
        assert connection.scalar(text("SELECT count(*) FROM public.business_credit_lines")) == 0
        connection.exec_driver_sql(
            "CREATE TEMP TABLE business_credits(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; "
            "CREATE TRIGGER fictional_credit_validation_history AFTER INSERT "
            "ON pg_temp.business_credits FOR EACH ROW "
            "EXECUTE FUNCTION public.coinpup_record_change()"
        )
        connection.execute(
            text("INSERT INTO pg_temp.business_credits VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_downgrade"
    assert snapshot(s) == before
