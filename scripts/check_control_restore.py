"""Fictional control facts for the guarded disposable bundle checker, never business APIs."""

from datetime import date
from uuid import uuid4

from coinpup_api.business.draft_schemas import DraftArchive, DraftCreate, DraftUpdate
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.models import (
    BusinessDocument,
    BusinessDocumentLine,
    BusinessParty,
    BusinessProject,
)
from coinpup_api.business.schemas import PartyCreate, ProjectCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.assets import get_asset
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.control_schemas import CONTROL_CLASSES
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerError, LedgerService
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
    project = uuid4()
    with controls._transaction(owner, write=True) as session:
        accounts = controls.ensure(session, owner, ledger, requirements)
        session.add(
            BusinessProject(
                id=project,
                ledger_id=ledger,
                name="Fictional 恢复项目",
                notes="Fictional project notes",
            )
        )
        session.flush()
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
                    project_id=project,
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
    draft, draft_line = uuid4(), uuid4()
    draft_input = DraftCreate(
        id=draft,
        document_kind="invoice",
        party_id=profiled_party,
        asset_id="USD",
        issue_date=date(2026, 10, 9),
        due_date=date(2026, 10, 20),
        notes="Fictional draft recovery 备注",
        lines=[
            dict(
                id=draft_line,
                description="Fictional 恢复服务",
                quantity="3.00",
                unit_price="19.99",
                discount_amount="9.97",
                tax_rate_percent="8.2500",
                category_id=categories["income"],
                project_id=project,
                recognition_date=date(2026, 9, 30),
            )
        ],
    )
    drafts = DraftService(engine)
    draft_response = drafts.create_draft(owner, ledger, draft_input)
    edit_body = draft_input.model_dump(exclude={"id"})
    edit_body["lines"][0]["quantity"] = "4.00"
    draft_edit = DraftUpdate(expected_version=1, **edit_body)
    draft_response = drafts.update_draft(owner, ledger, draft, draft_edit)
    drafts.set_draft_archived(owner, ledger, draft, DraftArchive(expected_version=2, archived=True))
    draft_response = drafts.set_draft_archived(
        owner, ledger, draft, DraftArchive(expected_version=3, archived=False)
    )
    assert draft_response.version == 4 and draft_response.lines[0].version == 2

    draft_values = draft_response.model_dump(
        exclude={
            "id",
            "ledger_id",
            "version",
            "created_at",
            "updated_at",
            "archived",
            "lines",
            "line_count",
            "net_amount",
            "tax_amount",
            "total_amount",
        }
    )
    line_values = draft_response.lines[0].model_dump(
        exclude={
            "id",
            "document_id",
            "ledger_id",
            "version",
            "created_at",
            "updated_at",
            "archived",
        }
    )
    for name in ("net_amount", "tax_amount", "total_amount"):
        line_values[name] = Amount.parse(line_values[name], get_asset("USD")).to_decimal()
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
        draft=draft,
        draft_input=draft_input,
        draft_edit=draft_edit,
        draft_response=draft_response,
        draft_line=draft_line,
        line_values=line_values,
        draft_values=draft_values,
        profile=profile,
        project=project,
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
    with engine.connect() as connection:
        project = (
            connection.execute(
                select(BusinessProject.__table__).where(BusinessProject.id == evidence["project"])
            )
            .mappings()
            .one()
        )
        assert project["ledger_id"] == ledger and project["version"] == 1
        assert (
            project["name"] == "Fictional 恢复项目"
            and project["notes"] == "Fictional project notes"
        )
    with engine.connect() as connection:
        draft = (
            connection.execute(
                select(BusinessDocument.__table__).where(BusinessDocument.id == evidence["draft"])
            )
            .mappings()
            .one()
        )
        assert {key: draft[key] for key in evidence["draft_values"]} == evidence["draft_values"]
        assert draft["ledger_id"] == ledger and draft["version"] == 4
    with engine.connect() as connection:
        line = (
            connection.execute(
                select(BusinessDocumentLine.__table__).where(
                    BusinessDocumentLine.id == evidence["draft_line"]
                )
            )
            .mappings()
            .one()
        )
        assert {key: line[key] for key in evidence["line_values"]} == evidence["line_values"]
        assert line["document_id"] == evidence["draft"] and line["ledger_id"] == ledger
        assert line["version"] == 2
    drafts = DraftService(engine)
    assert drafts.get_draft(owner, ledger, evidence["draft"]) == evidence["draft_response"]
    try:
        drafts.create_draft(owner, ledger, evidence["draft_input"])
    except LedgerError as error:
        assert error.code == "duplicate_record"
    else:
        raise AssertionError("Restored draft identity was recreated")
    assert drafts.get_draft(owner, ledger, evidence["draft"]) == evidence["draft_response"]
    try:
        drafts.update_draft(owner, ledger, evidence["draft"], evidence["draft_edit"])
    except LedgerError as error:
        assert error.code == "version_conflict"
    else:
        raise AssertionError("Restored stale draft edit overwrote the saved version")
    assert drafts.get_draft(owner, ledger, evidence["draft"]) == evidence["draft_response"]
    master = BusinessService(engine)
    for create, read, payload in (
        (
            master.create_party,
            master.get_party,
            PartyCreate(id=evidence["profiled_party"], **evidence["profile"]),
        ),
        (
            master.create_project,
            master.get_project,
            ProjectCreate(
                id=evidence["project"], name="Fictional 恢复项目", notes="Fictional project notes"
            ),
        ),
    ):
        before = read(owner, ledger, payload.id)
        try:
            create(owner, ledger, payload)
        except LedgerError as error:
            assert error.code == "duplicate_record"
        else:
            raise AssertionError("Restored stable master-data identity was recreated")
        assert read(owner, ledger, payload.id) == before
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
    history = posting.history(owner, ledger, evidence["operation"])
    assert history == evidence["history"]
    assert all(
        line.project_id == evidence["project"]
        for item in history
        for journal in item.journals
        for line in journal.lines
    )
