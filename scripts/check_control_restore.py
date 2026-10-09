"""Fictional control facts for the guarded disposable bundle checker, never business APIs."""

from datetime import date
from uuid import uuid4

from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine, BusinessParty
from coinpup_api.ledger.assets import get_asset
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.control_schemas import CONTROL_CLASSES
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import insert, select


def seed_controls(engine, owner):
    structure, controls = LedgerService(engine), ControlAccounts(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional control recovery",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger = entity.ledger.id
    categories = {row.kind: row.id for row in structure.list_categories(owner, ledger)}
    requirements = {key: {"USD"} for key in CONTROL_CLASSES}
    requirements["receivable.customer"].add("EUR")
    operations = []
    profile = dict(
        name="Fictional 示例往来",
        role="both",
        legal_name="Fictional Example Ltd",
        email="fictional@example.invalid",
        phone="Fictional phone",
        address="Fictional address",
        tax_identifier="FICTIONAL-NOT-A-TAX-ID",
        notes="Fictional restore 备注",
    )
    profiled_party = None
    with controls._transaction(owner, write=True) as session:
        accounts = controls.ensure(session, owner, ledger, requirements)
        # Two explicit fictional parties/documents distinguish otherwise matching balances.
        for index in range(2):
            party, document, line = uuid4(), uuid4(), uuid4()
            session.add_all(
                [
                    BusinessParty(id=party, ledger_id=ledger, **(profile if index == 0 else {})),
                    BusinessDocument(id=document, ledger_id=ledger),
                ]
            )
            session.flush()
            if index == 0:
                profiled_party = party
            session.add(BusinessDocumentLine(id=line, ledger_id=ledger, document_id=document))
            session.flush()
            for key in CONTROL_CLASSES if index == 0 else {"payable.supplier": "payable"}:
                negative = key in {"payable.supplier", "intercompany.payable", "advance.received"}
                kind = "expense" if negative else "income"
                value = "-100.00" if negative else "100.00"
                if index == 1:
                    value = "-40.00"
                operation, journal = uuid4(), uuid4()
                session.add(
                    FinancialOperation(
                        id=operation,
                        ledger_id=ledger,
                        kind=kind,
                        current_journal_id=journal,
                        created_by=owner,
                    )
                )
                session.flush()
                session.add(
                    Journal(
                        id=journal,
                        operation_id=operation,
                        ledger_id=ledger,
                        transaction_date=date(2026, 1, 1),
                        recognition_date=date(2026, 1, 1),
                        description="Fictional control recovery fact",
                    )
                )
                session.flush()
                quantity = Amount.parse(value, get_asset("USD"))
                dimensions = dict(
                    party_id=party,
                    document_id=document,
                    document_line_id=line,
                    counterparty_entity_id=entity.id,
                    dimension_owner_id=owner,
                )
                rows = [
                    PostingLine(
                        journal,
                        ledger,
                        1,
                        "account",
                        "USD",
                        quantity,
                        account_id=accounts[key].id,
                        **dimensions,
                    ),
                    PostingLine(
                        journal,
                        ledger,
                        2,
                        kind,
                        "USD",
                        -quantity,
                        category_id=categories[kind],
                        **dimensions,
                    ),
                ]
                session.execute(
                    insert(JournalLine), prepare_journal_lines(rows, {"USD": get_asset("USD")})
                )
                operations.append(operation)
    posting = PostingService(engine)
    cancellation = CancellationCreate(
        expected_version=1, reason="Fictional control recovery cancel"
    )
    receipt = posting.cancel_operation(
        owner, ledger, operations[0], cancellation, "fictional-control-recovery"
    )
    expected = controls.balances(owner, ledger)
    assert len(expected) == 8
    assert structure.list_accounts(owner, ledger) == [] and posting.balances(owner, ledger) == []
    return dict(
        ledger=ledger,
        profile=profile,
        profiled_party=profiled_party,
        expected=expected,
        operation=operations[0],
        cancellation=cancellation,
        receipt=receipt,
        history=posting.history(owner, ledger, operations[0]),
    )


def verify_controls(engine, owner, evidence):
    ledger = evidence["ledger"]
    controls, posting = ControlAccounts(engine), PostingService(engine)
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(BusinessParty.__table__).where(
                    BusinessParty.id == evidence["profiled_party"]
                )
            )
            .mappings()
            .one()
        )
        assert {key: row[key] for key in evidence["profile"]} == evidence["profile"]
        assert row["version"] == 1 and row["ledger_id"] == ledger
    assert controls.balances(owner, ledger) == evidence["expected"]
    assert LedgerService(engine).list_accounts(owner, ledger) == []
    assert posting.balances(owner, ledger) == []
    with controls._transaction(owner, write=True) as session:
        controls.ensure(session, owner, ledger, {"receivable.customer": {"USD", "EUR"}})
    assert (
        posting.cancel_operation(
            owner,
            ledger,
            evidence["operation"],
            evidence["cancellation"],
            "fictional-control-recovery",
        )
        == evidence["receipt"]
    )
    assert posting.history(owner, ledger, evidence["operation"]) == evidence["history"]
