"""Fictional complete credit revisions and immutable receipts in the private bundle."""

import json
from decimal import Decimal
from functools import partial
from uuid import uuid4

from check_refund_restore import classification
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.business.credit_revision_current import read_current_credit
from coinpup_api.business.credit_revision_history_reads import BusinessCreditHistory
from coinpup_api.business.credit_revision_reads import BusinessCreditRevisionReads
from coinpup_api.business.credit_revision_receipts import response_from_rows
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
)
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.schemas import PartyCreate, ProjectCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import CancellationCreate, CorrectionCreate
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import select


def frozen(payload, key, result=None):
    return dict(
        payload=payload,
        key=key,
        raw=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2).encode(),
        result=result,
    )


def invoke(service, method, owner, ledger, case, identifier=None):
    arguments = (owner, ledger) + (() if identifier is None else (identifier,))
    return getattr(service, method)(*arguments, case["payload"], case["key"], raw_body=case["raw"])


def observations(engine, owner, case):
    """Quiescent restore checks; each public service owns its authorized read-only snapshot."""
    ledger, original = case["ledger"], case["credit"]["result"]
    api_current = BusinessCreditRevisionReads(engine).current(owner, ledger, original.id)
    history = BusinessCreditHistory(engine)
    api_history = history.list_history(owner, ledger, original.id, limit=200)
    api_revisions = tuple(
        history.get_revision(owner, ledger, original.id, item["result"].revision_id).model_dump(
            mode="json"
        )
        for item in case["revisions"]
    )
    structure = LedgerService(engine)
    with structure._transaction(owner, read_only=True) as session:
        structure._ledger(session, owner, ledger)
        current = read_current_credit(session, ledger, original.id)
        fixed = tuple(
            response_from_rows(session, item["result"].revision_id).model_dump(mode="json")
            for item in case["revisions"]
        )
        identifiers = {item.operation_id for item in original.postings} | {
            member.operation_id for item in case["revisions"] for member in item["result"].members
        }
        operations = tuple(
            session.execute(
                select(
                    FinancialOperation.id,
                    FinancialOperation.version,
                    FinancialOperation.status,
                    FinancialOperation.current_journal_id,
                )
                .where(
                    FinancialOperation.ledger_id == ledger,
                    FinancialOperation.id.in_(identifiers),
                )
                .order_by(FinancialOperation.id)
            )
        )
        notifications = tuple(
            session.execute(
                select(ChangeLog.entity_version, ChangeLog.change_kind)
                .where(
                    ChangeLog.ledger_id == ledger,
                    ChangeLog.entity_type == "business_credits",
                    ChangeLog.entity_id == str(original.id),
                )
                .order_by(ChangeLog.seq)
            )
        )
        return dict(
            version=current.group_version,
            cancelled=current.cancelled,
            facts=tuple(dict(fact) for fact in current.facts),
            fixed=fixed,
            operations=operations,
            notifications=notifications,
            api_current=api_current.model_dump(mode="json"),
            api_history=tuple(entry.model_dump(mode="json") for entry in api_history),
            api_revisions=api_revisions,
        )


def seed_group(engine, owner, kind, files, store, pdf, upload):
    structure, master = LedgerService(engine), BusinessService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional credit revisions " + kind,
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger = entity.ledger.id
    bank = structure.create_account(
        owner, ledger, AccountCreate(name="Fictional unused bank", kind="bank", asset_ids=["USD"])
    )
    party = master.create_party(
        owner, ledger, PartyCreate(id=uuid4(), name="Fictional revision party", role="both")
    )
    project = master.create_project(
        owner, ledger, ProjectCreate(id=uuid4(), name="Fictional revision project")
    )
    categories = {row.kind: row.id for row in structure.list_categories(owner, ledger)}
    category = categories["income" if kind == "invoice" else "expense"]
    document = DraftService(engine).create_draft(
        owner,
        ledger,
        BusinessDraftCreate(
            id=uuid4(),
            document_kind=kind,
            party_id=party.id,
            asset_id="USD",
            issue_date="2026-10-10",
            lines=[
                dict(
                    id=uuid4(),
                    description="Fictional source " + label,
                    quantity="1",
                    unit_price="100.00",
                    tax_rate_percent="10",
                    category_id=category,
                    project_id=project.id,
                    recognition_date="2026-10-09",
                )
                for label in "ABC"
            ],
        ),
    )
    confirmation = frozen(
        BusinessConfirmationCreate(id=uuid4(), expected_version=document.version),
        "fictional-revision-confirm",
    )
    confirmation["result"] = invoke(
        BusinessConfirmationService(engine), "confirm", owner, ledger, confirmation, document.id
    )
    a, b, c = [line.id for line in document.lines]

    def lines(sources, day, net="17.23", tax="2.77"):
        return [
            dict(source_line_id=source, net_amount=net, tax_amount=tax, recognition_date=day)
            for source in sources
        ]

    creation = frozen(
        BusinessCreditCreate(
            id=uuid4(),
            document_id=document.id,
            expected_version=confirmation["result"].document_version,
            credit_date="2026-10-21",
            reason="Fictional original credit 中文",
            lines=lines([a, b], "2026-10-20"),
        ),
        "fictional-revision-credit",
    )
    original = creation["result"] = invoke(
        BusinessCreditService(engine), "create", owner, ledger, creation
    )
    detail = BusinessCreditReads(engine).get_credit(owner, ledger, original.id)
    money = PostingService(engine).balances(owner, ledger)
    revisions, originals = [], []
    # The last bill correction cancels the revision-born B while C remains active.
    for previous, sources in enumerate(([a, c], [b, c], None if kind == "invoice" else [a, c]), 1):
        action, day = ("cancel" if sources is None else "correct"), f"2026-11-0{previous + 3}"
        body = dict(expected_version=previous, reason=f"Fictional group revision {previous} 中文")
        if sources is not None:
            body["replacement"] = dict(
                credit_date=day,
                reason=f"Fictional replacement {previous} 中文",
                lines=lines(
                    sources,
                    day,
                    "18.10" if previous == 2 else "17.23",
                    "1.90" if previous == 2 else "2.77",
                ),
            )
        revision = frozen(
            (CreditCorrectionCreate if sources is not None else CreditCancellationCreate)(**body),
            f"fictional-revision-{previous}",
        ) | {"action": action}
        revision["result"] = invoke(
            BusinessCreditRevisionService(engine), action, owner, ledger, revision, original.id
        )
        revisions.append(revision)
        if kind == "invoice" and previous == 1:
            created = next(item for item in revision["result"].members if item.action == "create")
            originals.append(
                upload(
                    files,
                    store,
                    owner,
                    ledger,
                    pdf,
                    "Fictional credit adjustment evidence.pdf",
                    operation=created.operation_id,
                )
            )
    case = dict(
        ledger=ledger,
        entity=entity.id,
        archived=kind == "invoice",
        document=document.id,
        kind=kind,
        sources=(a, b, c),
        confirmation=confirmation,
        credit=creation,
        revisions=revisions,
        originals=originals,
        detail=detail,
        money=money,
        replacement=dict(
            kind="expense",
            account_id=bank.id,
            asset_id="USD",
            amount="1.00",
            transaction_date="2026-11-06",
            recognition_date="2026-11-06",
            splits=[dict(category_id=categories["expense"], amount="1.00")],
        ),
    )
    expected = observations(engine, owner, case)
    api_current = expected["api_current"]
    if (
        (api_current["credit_id"], api_current["ledger_id"], api_current["document_id"])
        != (str(original.id), str(ledger), str(document.id))
        or api_current["version"] != expected["version"]
        or (api_current["status"] == "cancelled") != expected["cancelled"]
        or [
            (
                row["source_line_id"],
                row["operation_id"],
                row["operation_version"],
                row["journal_id"],
            )
            for row in api_current["members"]
        ]
        != [
            (
                str(row["source_line_id"]),
                str(row["operation_id"]),
                row["operation_version"],
                str(row["journal_id"]),
            )
            for row in expected["facts"]
        ]
        or [
            (row["version"], row["action"], row["revision_id"], row["reason"])
            for row in expected["api_history"]
        ]
        != [(1, "create", None, creation["payload"].reason)]
        + [
            (
                item["result"].version,
                item["action"],
                str(item["result"].revision_id),
                item["result"].reason,
            )
            for item in revisions
        ]
        or expected["api_revisions"] != expected["fixed"]
    ):
        raise RuntimeError("Public credit reads differ from the complete group or frozen receipts")
    if expected["version"] != 4 or expected["notifications"] != (
        (1, "upsert"),
        (2, "upsert"),
        (3, "upsert"),
        (4, "cancel" if case["archived"] else "upsert"),
    ):
        raise RuntimeError("Fictional credit group versions or stable notifications are incomplete")
    latest = revisions[-1]["result"]
    if expected["fixed"] != tuple(item["result"].model_dump(mode="json") for item in revisions):
        raise RuntimeError("Fixed historical credit responses differ after later revisions")
    if [fact["source_line_id"] for fact in expected["facts"]] != (
        [] if case["archived"] else [a, c]
    ):
        raise RuntimeError("Fictional current group resurrected a historical member")
    capacity = BusinessCreditReads(engine).capacity(owner, ledger, document.id)
    for line in capacity.lines:
        credited = not case["archived"] and line.source_line_id in {a, c}
        if (line.credited_net_amount, line.credited_tax_amount, line.available_gross_amount) != (
            ("17.23", "2.77", "90.00") if credited else ("0.00", "0.00", "110.00")
        ):
            raise RuntimeError("Fictional revision capacity double-counted historical net or tax")
    # Read the exact signed gross total without performing Decimal arithmetic.
    totals = classification(engine, ledger)
    role = "income" if kind == "invoice" else "expense"
    if (
        totals[("USD", role)] != Decimal("-330" if kind == "invoice" else "290")
        or PostingService(engine).balances(owner, ledger) != money
    ):
        raise RuntimeError("Fictional revisions changed cash or incorrectly reduced gross activity")
    if (latest.action == "cancel") != expected["cancelled"]:
        raise RuntimeError("Fictional cancellation did not retain its terminal group")
    period = PeriodChange(
        action="close",
        expected_version=1,
        closed_through="2026-11-06",
        reason="Fictional credit revision restore cutoff",
    )
    PeriodService(engine).change(owner, ledger, period, "fictional-revision-close")
    if case["archived"]:
        structure.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
    case.update(
        expected=expected,
        capacity=capacity,
        classification=totals,
        controls=ControlAccounts(engine).balances(owner, ledger),
        confirmation_detail=BusinessConfirmationReads(engine).get_confirmation(
            owner, ledger, document.id
        ),
        links=[
            files.list_operation_files(owner, ledger, original["request"].operation_id)
            for original in originals
        ],
    )
    return case


def seed_credit_revisions(engine, owner, files, store, pdf, upload):
    groups = [
        seed_group(engine, owner, kind, files, store, pdf, upload) for kind in ("invoice", "bill")
    ]
    return dict(
        owner=owner,
        groups=groups,
        originals=[item for case in groups for item in case["originals"]],
    )


def rejected(call, expected=("not_found", 404)):
    try:
        call()
    except LedgerError as error:
        if (error.code, error.status) == expected:
            return
        raise RuntimeError(
            "Restored private credit isolation returned a different rejection"
        ) from error
    raise RuntimeError("Restored private credit member escaped its dedicated command boundary")


def verify_credit_revisions(engine, evidence):
    owner, files = evidence["owner"], DocumentService(engine)
    for case in evidence["groups"]:
        ledger, original = case["ledger"], case["credit"]["result"]
        reads, posting = BusinessCreditReads(engine), PostingService(engine)
        if (
            observations(engine, owner, case) != case["expected"]
            or reads.get_credit(owner, ledger, original.id) != case["detail"]
            or reads.capacity(owner, ledger, case["document"]) != case["capacity"]
            or classification(engine, ledger) != case["classification"]
            or ControlAccounts(engine).balances(owner, ledger) != case["controls"]
            or posting.balances(owner, ledger) != case["money"]
            or BusinessConfirmationReads(engine).get_confirmation(owner, ledger, case["document"])
            != case["confirmation_detail"]
        ):
            raise RuntimeError(
                "Restored current credit group or immutable source observations differ"
            )
        if (
            invoke(BusinessCreditService(engine), "create", owner, ledger, case["credit"])
            != original
        ):
            raise RuntimeError("Restored original credit 201 receipt changed")
        if (
            invoke(
                BusinessConfirmationService(engine),
                "confirm",
                owner,
                ledger,
                case["confirmation"],
                case["document"],
            )
            != case["confirmation"]["result"]
        ):
            raise RuntimeError("Restored original confirmation receipt changed")
        for revision in case["revisions"]:
            if (
                invoke(
                    BusinessCreditRevisionService(engine),
                    revision["action"],
                    owner,
                    ledger,
                    revision,
                    original.id,
                )
                != revision["result"]
            ):
                raise RuntimeError("Restored historical credit 200 receipt changed")
        created = {
            item.operation_id
            for revision in case["revisions"]
            for item in revision["result"].members
            if item.action == "create"
        }
        for operation, version, _status, _journal in case["expected"]["operations"]:
            if operation not in created:
                continue
            rejected(partial(posting.get_operation, owner, ledger, operation))
            rejected(partial(posting.history, owner, ledger, operation))
            for method, payload in (
                (
                    "correct_operation",
                    CorrectionCreate(
                        expected_version=version,
                        reason="Fictional forbidden child",
                        replacement=case["replacement"],
                    ),
                ),
                (
                    "cancel_operation",
                    CancellationCreate(
                        expected_version=version, reason="Fictional forbidden child"
                    ),
                ),
            ):
                # Existing archived-entity precedence is preserved, not mistaken for kind isolation.
                rejected(
                    partial(
                        getattr(posting, method),
                        owner,
                        ledger,
                        operation,
                        payload,
                        f"fictional-forbidden-{method}-{operation}",
                    ),
                    ("entity_archived", 409) if case["archived"] else ("not_found", 404),
                )
        for fixture, links in zip(case["originals"], case["links"], strict=True):
            if files.list_operation_files(owner, ledger, fixture["request"].operation_id) != links:
                raise RuntimeError("Restored cancelled new credit member lost its private evidence")
            other = next(item["ledger"] for item in evidence["groups"] if item["ledger"] != ledger)
            rejected(partial(files.get_download, owner, other, fixture["receipt"].file_id))
