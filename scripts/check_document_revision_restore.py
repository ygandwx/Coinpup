"""Restore complete fictional document histories, exact original intents and private evidence."""

from uuid import uuid4

from check_credit_revision_restore import frozen, invoke
from coinpup_api.business.confirmation_reads import BusinessConfirmationReads
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.business.credit_revision_history_reads import BusinessCreditHistory
from coinpup_api.business.credit_revision_reads import BusinessCreditRevisionReads
from coinpup_api.business.credit_revision_schemas import CreditCancellationCreate
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.business.document_fixed_receipts import fixed_document_response
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
    ConfirmedDocumentReplacement,
)
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.business.document_revisions import BusinessDocumentRevisionService
from coinpup_api.business.draft_schemas import (
    BusinessDraftCreate,
    BusinessDraftLineInput,
    DraftHeaderInput,
)
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.schemas import EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import select


def current(engine, owner, ledger, identifier):
    service = LedgerService(engine)
    with service._transaction(owner, read_only=True) as db:
        service._ledger(db, owner, ledger)
        return read_confirmed_document_version(db, ledger, identifier)


def observations(engine, owner, case):
    ledger, identifier = case["ledger"], case["document"]
    service = LedgerService(engine)
    with service._transaction(owner, read_only=True) as db:
        service._ledger(db, owner, ledger)
        now = read_confirmed_document_version(db, ledger, identifier)
        versions = [case["confirmation"]["result"].document_version] + [
            row["result"].version for row in case["revisions"]
        ]
        details = []
        for version in versions:
            saved = read_confirmed_document_version(db, ledger, identifier, version=version)
            details.append(
                (version, saved.document.model_dump(mode="json") if saved.document else None)
            )
        fixed = [
            fixed_document_response(db, ledger, identifier, row["result"].revision_id).model_dump(
                mode="json"
            )
            for row in case["revisions"]
        ]
        operations = db.execute(
            select(
                FinancialOperation.id,
                FinancialOperation.version,
                FinancialOperation.status,
                FinancialOperation.current_journal_id,
            )
            .where(FinancialOperation.ledger_id == ledger)
            .order_by(FinancialOperation.id)
        ).all()
        return dict(
            version=now.version,
            cancelled=now.document is None,
            details=details,
            fixed=fixed,
            operations=operations,
            original=now.original.model_dump(mode="json"),
            credits=[
                (
                    BusinessCreditReads(engine).get_credit(owner, ledger, saved["result"].id),
                    BusinessCreditRevisionReads(engine).current(owner, ledger, saved["result"].id),
                    BusinessCreditHistory(engine).list_history(owner, ledger, saved["result"].id),
                )
                for saved in case["credits"]
            ],
        )


def seed_document_revisions(engine, owner, files, store, pdf, upload):
    cases, originals = [], []
    for kind, asset, unit in (
        ("invoice", "BTC", "0.00000001"),
        ("bill", "ETH", "0.000000000000000001"),
    ):
        structure = LedgerService(engine)
        entity = structure.create_entity(
            owner,
            EntityCreate(
                kind="personal",
                name="Fictional document revision bundle " + kind,
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        ledger = entity.ledger.id
        party = BusinessService(engine).create_party(
            owner,
            ledger,
            PartyCreate(id=uuid4(), name="Fictional frozen document party", role="both"),
        )
        category = next(
            r.id
            for r in structure.list_categories(owner, ledger)
            if r.kind == ("income" if kind == "invoice" else "expense")
        )
        first = dict(
            id=uuid4(),
            description="Fictional preserved source",
            quantity="1",
            unit_price="0.01",
            tax_rate_percent="0",
            category_id=category,
            recognition_date="2026-10-01",
        )
        draft = DraftService(engine).create_draft(
            owner,
            ledger,
            BusinessDraftCreate(
                id=uuid4(),
                document_kind=kind,
                party_id=party.id,
                asset_id="USD",
                issue_date="2026-10-02",
                lines=[
                    first,
                    first | dict(id=uuid4(), unit_price="0"),
                    first | dict(id=uuid4(), unit_price="0.02"),
                ],
            ),
        )
        confirmation = frozen(
            BusinessConfirmationCreate(id=uuid4(), expected_version=draft.version),
            "fictional-document-origin-" + kind,
        )
        confirmation["result"] = invoke(
            BusinessConfirmationService(engine), "confirm", owner, ledger, confirmation, draft.id
        )
        doc = current(engine, owner, ledger, draft.id).document
        original_detail = BusinessConfirmationReads(engine).get_confirmation(
            owner, ledger, draft.id
        )
        old = [r.model_dump(include=set(BusinessDraftLineInput.model_fields)) for r in doc.lines]
        proposed = ConfirmedDocumentReplacement(
            **(doc.model_dump(include=set(DraftHeaderInput.model_fields)) | dict(asset_id=asset)),
            lines=[
                old[0] | dict(unit_price=unit),
                old[1] | dict(unit_price=unit, quantity="2"),
                old[2] | dict(id=uuid4(), unit_price=unit),
                old[2] | dict(id=uuid4(), unit_price="0"),
            ],
            reviewed_references=[dict(kind="category", id=category, version=1)],
        )
        service = BusinessDocumentRevisionService(engine)
        one = frozen(
            ConfirmedDocumentCorrection(
                expected_version=doc.version,
                reason="Fictional asset and zero-line correction",
                replacement=proposed,
            ),
            "fictional-document-revision-one",
        )
        one["action"] = "correct"
        one["result"] = invoke(service, "correct", owner, ledger, one, draft.id)

        def create_credit(version, source, key, *, document_id=draft.id, unit=unit, ledger=ledger):
            saved = frozen(
                BusinessCreditCreate(
                    id=uuid4(),
                    document_id=document_id,
                    expected_version=version,
                    credit_date="2026-10-02",
                    reason="Fictional version-bound credit restore",
                    lines=[
                        dict(
                            source_line_id=source,
                            net_amount=unit,
                            tax_amount="0",
                            recognition_date="2026-10-01",
                        )
                    ],
                ),
                key,
            )
            saved["result"] = invoke(BusinessCreditService(engine), "create", owner, ledger, saved)
            return saved

        early_credit = create_credit(one["result"].version, old[1]["id"], "fictional-early-credit")
        released_credit = frozen(
            CreditCancellationCreate(
                expected_version=1, reason="Fictional explicit bound-credit release"
            ),
            "fictional-bound-credit-cancel",
        )
        released_credit["result"] = invoke(
            BusinessCreditRevisionService(engine),
            "cancel",
            owner,
            ledger,
            released_credit,
            early_credit["result"].id,
        )
        credits = [early_credit]
        born = one["result"].replacement.lines[1].posting.operation_id
        attachment = upload(
            files,
            store,
            owner,
            ledger,
            pdf,
            "Fictional document revision evidence.pdf",
            operation=born,
        )
        originals.append(attachment)
        doc = current(engine, owner, ledger, draft.id).document
        keep = [
            r.model_dump(include=set(BusinessDraftLineInput.model_fields))
            for r in doc.lines
            if r.id != old[1]["id"]
        ]
        two = frozen(
            ConfirmedDocumentCorrection(
                expected_version=doc.version,
                reason="Fictional retire new accrual and retain evidence",
                replacement=ConfirmedDocumentReplacement(
                    **doc.model_dump(include=set(DraftHeaderInput.model_fields)), lines=keep
                ),
            ),
            "fictional-document-revision-two",
        )
        two["action"] = "correct"
        two["result"] = invoke(service, "correct", owner, ledger, two, draft.id)
        revisions = [one, two]
        if kind == "bill":
            credits.append(
                create_credit(two["result"].version, old[0]["id"], "fictional-latest-active-credit")
            )
        if kind == "invoice":
            end = frozen(
                ConfirmedDocumentCancellation(
                    expected_version=two["result"].version, reason="Fictional terminal document"
                ),
                "fictional-document-revision-end",
            )
            end["action"] = "cancel"
            end["result"] = invoke(service, "cancel", owner, ledger, end, draft.id)
            revisions.append(end)
        with structure._transaction(owner, read_only=True) as db:
            retired = db.get(FinancialOperation, born)
            if retired.status != "cancelled" or retired.version != 2:
                raise RuntimeError("Fictional revision-born operation was not retired")
        PeriodService(engine).change(
            owner,
            ledger,
            PeriodChange(
                action="close",
                closed_through="2026-11-06",
                expected_version=1,
                reason="Fictional complete historical period",
            ),
            "fictional-document-period",
        )
        structure.update_entity(owner, entity.id, EntityUpdate(expected_version=1, archived=True))
        case = dict(
            ledger=ledger,
            document=draft.id,
            confirmation=confirmation,
            revisions=revisions,
            original_detail=original_detail,
            attachment=attachment,
            born=born,
            credits=credits,
            released_credit=released_credit,
        )
        case["expected"] = observations(engine, owner, case)
        cases.append(case)
    evidence = dict(owner=owner, cases=cases, originals=originals)
    verify_document_revisions(engine, evidence)
    return evidence


def verify_document_revisions(engine, evidence):
    owner = evidence["owner"]
    for case in evidence["cases"]:
        ledger, identifier = case["ledger"], case["document"]
        if (
            invoke(
                BusinessConfirmationService(engine),
                "confirm",
                owner,
                ledger,
                case["confirmation"],
                identifier,
            )
            != case["confirmation"]["result"]
        ):
            raise RuntimeError("Original document201 changed after restore")
        for revision in case["revisions"]:
            if (
                invoke(
                    BusinessDocumentRevisionService(engine),
                    revision["action"],
                    owner,
                    ledger,
                    revision,
                    identifier,
                )
                != revision["result"]
            ):
                raise RuntimeError("Fixed document200 changed after restore")
        for saved in case["credits"]:
            if (
                invoke(BusinessCreditService(engine), "create", owner, ledger, saved)
                != saved["result"]
            ):
                raise RuntimeError("Version-bound original credit201 changed after restore")
        released = case["released_credit"]
        credit_id = case["credits"][0]["result"].id
        if (
            invoke(
                BusinessCreditRevisionService(engine), "cancel", owner, ledger, released, credit_id
            )
            != released["result"]
            or BusinessCreditHistory(engine).get_revision(
                owner, ledger, credit_id, released["result"].revision_id
            )
            != released["result"]
        ):
            raise RuntimeError("Version-bound credit200 changed after restore")
        if (
            observations(engine, owner, case) != case["expected"]
            or BusinessConfirmationReads(engine).get_confirmation(owner, ledger, identifier)
            != case["original_detail"]
        ):
            raise RuntimeError("Historical/current document evidence changed after restore")
        files = DocumentService(engine)
        links = files.list_operation_files(owner, ledger, case["born"])
        if len(links) != 1 or links[0].file_id != case["attachment"]["receipt"].file_id:
            raise RuntimeError("Retired document accrual lost its private evidence")
        try:
            files.get_download(owner, uuid4(), case["attachment"]["receipt"].file_id)
        except LedgerError as error:
            if (error.status, error.code) != (404, "not_found"):
                raise
        else:
            raise RuntimeError("Document evidence escaped its ledger")
