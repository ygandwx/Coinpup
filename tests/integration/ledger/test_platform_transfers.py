"""Platform groups retain separately balanced originals, reversals, and frozen receipts."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from coinpup_api.ledger.assets import TOKEN_PRESETS, AssetDefinition, custodial_family, get_asset
from coinpup_api.ledger.models import (
    CommandReceipt,
    FinancialOperation,
    JournalLine,
    PlatformTransfer,
)
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.platform_transfer_schemas import (
    PlatformTransferCancel,
    PlatformTransferCreate,
)
from coinpup_api.ledger.platform_transfers import PlatformTransfers
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetCreate,
    CategoryCreate,
    EntityCreate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.reporting.service import MvpReportService
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration
_DAY = "2026-11-27"


def _ledger(service, owner, name):
    return service.create_entity(
        owner, EntityCreate(kind="personal", name=name, base_asset_id="USD")
    ).ledger.id


def _account(service, owner, ledger, mode, asset):
    return service.create_account(
        owner,
        ledger,
        AccountCreate(
            kind="crypto",
            name=f"Fictional {mode or 'legacy'}",
            custody_mode=mode,
            asset_ids=[asset],
        ),
    )


def _scenario(engine, owner, *, same=False, code="USDT", network="tron"):
    service, moves = LedgerService(engine), PlatformTransfers(engine)
    chain_definition = next(
        row.definition
        for row in TOKEN_PRESETS
        if row.definition.code == code and row.definition.network == network
    )
    chain = service.create_asset(
        owner,
        AssetCreate(
            code=code,
            kind="token",
            scale=chain_definition.scale,
            network=network,
            token_reference=chain_definition.token_reference,
        ),
    ).asset_id
    custody_definition = get_asset(f"custodial:{code}")
    custody = service.create_asset(
        owner, AssetCreate(code=code, kind="custodial", scale=custody_definition.scale)
    ).asset_id
    first = _ledger(service, owner, "Fictional source company")
    second = first if same else _ledger(service, owner, "Fictional destination company")
    onchain = _account(service, owner, first, "onchain", chain)
    platform = _account(service, owner, second, "custodial", custody)
    return service, moves, first, second, onchain, platform, chain, custody


def _deposit(scenario, **changes):
    _, _, _, target, onchain, platform, chain, custody = scenario
    values = {
        "kind": "deposit",
        "source_account_id": onchain.id,
        "source_asset_id": chain,
        "destination_ledger_id": target,
        "destination_account_id": platform.id,
        "destination_asset_id": custody,
        "amount": "2.123456",
        "transaction_date": _DAY,
        "description": "Fictional transfer",
    }
    return PlatformTransferCreate(**(values | changes))


def _closing(engine, owner, ledger, *, reopen=False):
    PeriodService(engine).change(
        owner,
        ledger,
        PeriodChange(
            action="reopen" if reopen else "close",
            closed_through=None if reopen else _DAY,
            expected_version=2 if reopen else 1,
            reason="Fictional closing rule",
        ),
        "reopen-platform" if reopen else "close-platform",
    )


@pytest.mark.parametrize("same", [False, True])
def test_deposit_preserves_clearing_fee_links_and_exact_whole_reversal(structure_database, same):
    engine, owner = structure_database
    scenario = _scenario(engine, owner, same=same)
    service, moves, first, second, onchain, platform, chain, custody = scenario
    fee_account = service.create_account(
        owner, first, AccountCreate(kind="cash", name="Fictional fee account", asset_ids=["USD"])
    )
    category = service.create_category(
        owner, first, CategoryCreate(name="Fictional network expense", kind="expense")
    )
    body = _deposit(
        scenario,
        fees=[
            {
                "account_id": fee_account.id,
                "asset_id": "USD",
                "category_id": category.id,
                "amount": "0.25",
            }
        ],
    )
    receipt = moves.create(owner, first, body, "deposit")
    assert len(receipt.operations) == (1 if same else 2)
    assert receipt.source_amount == receipt.destination_amount == "2.123456"
    assert moves.list(owner, second)[0].id == receipt.id
    assert moves.balances(owner, first, onchain.id)[0].amount == "-2.123456"
    assert moves.balances(owner, second, platform.id)[0].amount == "2.123456"
    report = MvpReportService(engine).read(owner, tuple({first, second}), "cash")
    assert {(item.asset_id, item.amount) for item in report.originals} == {("USD", "-0.25")}
    assert len([row for row in report.rows if row.excluded_platform_transfer == receipt.id]) == 2
    assert all(
        row.platform_transfer_id == receipt.id for row in report.rows if row.asset_id != "USD"
    )
    if not same:
        one_side = MvpReportService(engine).read(owner, (second,), "cash")
        assert [(item.asset_id, item.amount) for item in one_side.originals] == [
            (custody, "2.123456")
        ]

    journal_ids = [item.journal_id for item in receipt.operations]
    with engine.connect() as connection:
        totals = connection.execute(
            select(JournalLine.journal_id, JournalLine.asset_id, func.sum(JournalLine.amount))
            .where(JournalLine.journal_id.in_(journal_ids))
            .group_by(JournalLine.journal_id, JournalLine.asset_id)
        ).all()
        assert {asset for _, asset, _ in totals} == {chain, custody, "USD"}
        assert all(amount == 0 for _, _, amount in totals)
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalLine)
                .where(JournalLine.journal_id.in_(journal_ids), JournalLine.role == "expense")
            )
            == 1
        )

    cancel = PlatformTransferCancel(expected_version=1, reason="Fictional return to original")
    cancellation = moves.cancel(owner, first, receipt.id, cancel, "cancel")
    assert len(cancellation.reversals) == len(receipt.operations)
    assert moves.detail(owner, second, receipt.id).cancellation == cancellation
    assert moves.create(owner, first, body, "deposit") == receipt
    assert moves.cancel(owner, first, receipt.id, cancel, "cancel") == cancellation
    with pytest.raises(LedgerError, match="already cancelled"):
        moves.cancel(
            owner, first, receipt.id, cancel.model_copy(update={"expected_version": 2}), "again"
        )
    assert moves.balances(owner, first, onchain.id)[0].amount == "0.000000"
    assert moves.balances(owner, second, platform.id)[0].amount == "0.000000"
    after = MvpReportService(engine).read(owner, tuple({first, second}), "cash")
    assert {item.asset_id for item in after.originals} == {"USD"}
    assert len([row for row in after.rows if row.excluded_platform_transfer == receipt.id]) == 4


def test_withdrawal_rejects_target_precision_and_symbol_lookalikes(structure_database):
    engine, owner = structure_database
    scenario = _scenario(engine, owner, code="FDUSD", network="solana")
    service, moves, wallet_ledger, platform_ledger, wallet, platform, chain, custody = scenario
    body = PlatformTransferCreate(
        kind="withdrawal",
        source_account_id=platform.id,
        source_asset_id=custody,
        destination_ledger_id=wallet_ledger,
        destination_account_id=wallet.id,
        destination_asset_id=chain,
        transaction_date=_DAY,
        amount="0.0000001",
    )
    with pytest.raises(LedgerError) as overprecise:
        moves.create(owner, platform_ledger, body, "overprecision")
    assert overprecise.value.code == "amount_precision"
    fake = AssetDefinition("FDUSD", "token", 6, "solana", "FictionalUnverifiedContract")
    assert custodial_family(fake) is None
    custom = service.create_asset(
        owner,
        AssetCreate(
            code=fake.code,
            kind=fake.kind,
            scale=fake.scale,
            network=fake.network,
            token_reference=fake.token_reference,
        ),
    )
    other = _account(service, owner, wallet_ledger, "onchain", custom.asset_id)
    body = body.model_copy(update={"amount": "0.000001"})
    with pytest.raises(LedgerError) as unverified:
        moves.create(
            owner,
            platform_ledger,
            body.model_copy(
                update={"destination_account_id": other.id, "destination_asset_id": custom.asset_id}
            ),
            "unverified",
        )
    assert unverified.value.code == "platform_asset_mismatch"
    receipt = moves.create(owner, platform_ledger, body, "withdrawal")
    assert receipt.source_amount == "0.000001000000000000"
    assert receipt.destination_amount == "0.000001"


def test_closed_period_and_destination_scope_are_atomic(structure_database):
    engine, owner = structure_database
    scenario = _scenario(engine, owner)
    service, moves, first, second, _, _, _, _ = scenario
    body = _deposit(scenario)
    unrelated_ledger = _ledger(service, owner, "Fictional unrelated ledger")
    with pytest.raises(LedgerError) as unrelated:
        moves.create(
            owner,
            first,
            body.model_copy(update={"destination_ledger_id": unrelated_ledger}),
            "wrong-scope",
        )
    assert unrelated.value.status == 404
    _closing(engine, owner, second)
    with pytest.raises(LedgerError) as closed:
        moves.create(owner, first, body, "deposit")
    assert closed.value.code == "period_closed"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 0
        assert connection.scalar(select(func.count()).select_from(CommandReceipt)) == 0
        assert connection.scalar(select(func.count()).select_from(PlatformTransfer)) == 0
    _closing(engine, owner, second, reopen=True)
    receipt = moves.create(owner, first, body, "deposit")
    with pytest.raises(LedgerError) as conflict:
        moves.create(owner, first, body.model_copy(update={"amount": "2.1"}), "deposit")
    assert conflict.value.code == "idempotency_conflict"
    _closing_again = PeriodChange(
        action="close", closed_through=_DAY, expected_version=3, reason="Fictional second close"
    )
    PeriodService(engine).change(owner, second, _closing_again, "close-again")
    with pytest.raises(LedgerError) as refused:
        moves.cancel(
            owner,
            first,
            receipt.id,
            PlatformTransferCancel(expected_version=1, reason="Fictional"),
            "cancel",
        )
    assert refused.value.code == "period_closed"
    assert moves.detail(owner, second, receipt.id).status == "active"
    assert moves.create(owner, first, body, "deposit") == receipt


def test_direct_database_cannot_commit_single_member_reversal(structure_database):
    engine, owner = structure_database
    scenario = _scenario(engine, owner)
    _, moves, first, _, _, _, _, _ = scenario
    receipt = moves.create(owner, first, _deposit(scenario), "deposit")
    opening = receipt.operations[0]
    params = {"operation": opening.operation_id, "original": opening.journal_id, "new": uuid4()}
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO journals (id, operation_id, ledger_id, operation_version, journal_kind,
                                      reverses_journal_id, transaction_date, recognition_date,
                                      description, revision_reason)
                SELECT :new, operation_id, ledger_id, 2, 'reversal', id, transaction_date,
                       recognition_date, description, 'Fictional raw reversal'
                FROM journals WHERE id=:original
            """),
                params,
            )
            connection.execute(
                text("""
                INSERT INTO journal_lines (id, journal_id, ledger_id, line_no, component_no,
                                           role, asset_id, account_id, category_id, amount)
                SELECT gen_random_uuid(), :new, ledger_id, line_no, component_no, role,
                       asset_id, account_id, category_id, -amount
                FROM journal_lines WHERE journal_id=:original
            """),
                params,
            )
            connection.execute(
                text("""
                UPDATE financial_operations SET status='cancelled', version=2,
                    updated_at=clock_timestamp()
                WHERE id=:operation
            """),
                params,
            )
    assert rejected.value.orig.diag.constraint_name == "ck_platform_transfer_members"
    assert moves.detail(owner, first, receipt.id).status == "active"


def test_direct_database_cannot_reuse_members_in_opposite_columns(structure_database):
    engine, owner = structure_database
    scenario = _scenario(engine, owner)
    _, moves, first, second, onchain, platform, chain, custody = scenario
    receipt = moves.create(owner, first, _deposit(scenario), "deposit")
    source, destination = receipt.operations
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            connection.execute(
                insert(CommandReceipt).values(
                    ledger_id=second,
                    key="fictional-alternate",
                    request_hash="0" * 64,
                    hash_version=2,
                    response={},
                    response_status=201,
                    operation_id=destination.operation_id,
                )
            )
            connection.execute(
                insert(PlatformTransfer).values(
                    id=uuid4(),
                    owner_id=owner,
                    kind="withdrawal",
                    source_ledger_id=second,
                    source_account_id=platform.id,
                    source_asset_id=custody,
                    source_operation_id=destination.operation_id,
                    destination_ledger_id=first,
                    destination_account_id=onchain.id,
                    destination_asset_id=chain,
                    destination_operation_id=source.operation_id,
                    amount=Decimal(receipt.source_amount),
                    transaction_date=receipt.transaction_date,
                    description="Fictional duplicate",
                    version=1,
                    create_key="fictional-alternate",
                )
            )
    assert rejected.value.orig.diag.constraint_name == "ck_platform_transfer_members"
    assert moves.list(owner, second)[0].id == receipt.id


def test_legacy_classification_and_database_downgrade_protection(structure_database):
    engine, owner = structure_database
    service, _, first, _, _, _, chain, custody = _scenario(engine, owner)
    legacy = _account(service, owner, first, None, chain)
    updated = service.update_account(
        owner, first, legacy.id, AccountUpdate(expected_version=1, custody_mode="onchain")
    )
    assert updated.custody_mode == "onchain" and updated.version == 2
    with pytest.raises(LedgerError) as wrong_account:
        _account(service, owner, first, "custodial", chain)
    assert wrong_account.value.code == "custody_asset_mismatch"
    with pytest.raises(LedgerError) as wrong_custody:
        _account(service, owner, first, None, custody)
    assert wrong_custody.value.code == "custody_asset_mismatch"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    with pytest.raises(IntegrityError) as downgrade:
        command.downgrade(cfg, "20261127_0115")
    assert downgrade.value.orig.diag.constraint_name == "ck_platform_transfers_downgrade"


@pytest.mark.slow
def test_concurrent_duplicate_writes_keep_one_group_and_receipt(structure_database):
    engine, owner = structure_database
    scenario = _scenario(engine, owner)
    _, moves, first, _, _, _, _, _ = scenario
    body = _deposit(scenario)
    with ThreadPoolExecutor(2) as workers:
        results = list(
            workers.map(lambda _: moves.create(owner, first, body, "same-key"), range(2))
        )
    assert results[0] == results[1]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(PlatformTransfer)) == 1
