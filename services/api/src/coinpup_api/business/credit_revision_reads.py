"""Read current complete credit facts in one authorized, bounded read-only snapshot."""

from pydantic import ValidationError
from sqlalchemy import select, true
from sqlalchemy.orm import aliased

from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_projections import posting_facts
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.credit_revision_read_schemas import CreditCurrent, CreditCurrentMember
from coinpup_api.business.credit_revision_receipts import integrity
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerService, _not_found


def _current(credit, document, asset, revision, facts):
    """Validate the bounded projection without re-reading history or today's availability."""
    version = 1 if revision is None else revision.group_version
    identifier = None if revision is None else revision.id
    cancelled = revision is not None and revision.action == "cancel"
    day = credit.credit_date if revision is None else revision.credit_date
    reason = credit.reason if revision is None else revision.credit_reason
    if revision is not None and (
        (revision.credit_id, revision.document_id, revision.ledger_id)
        != (credit.id, document.id, credit.ledger_id)
        or revision.action not in {"correct", "cancel"}
        or version < 2
    ):
        raise integrity()
    members = []
    try:
        for fact in facts:
            if (
                (fact["ledger_id"], fact["credit_id"], fact["document_id"], fact["revision_id"])
                != (credit.ledger_id, credit.id, document.id, identifier)
                or fact["group_version"] != version
                or fact["credit_date"] != day
                or fact["credit_reason"] != reason
            ):
                raise integrity()
            net, tax, total = (
                Amount.from_decimal(fact[field], asset)
                for field in ("net_amount", "tax_amount", "total_amount")
            )
            if (
                min(net.minor_units, tax.minor_units) < 0
                or total.minor_units <= 0
                or net + tax != total
            ):
                raise integrity()
            members.append(
                CreditCurrentMember(
                    **{
                        field: fact[field]
                        for field in CreditCurrentMember.model_fields
                        if field not in {"net_amount", "tax_amount", "total_amount"}
                    },
                    net_amount=net.to_string(),
                    tax_amount=tax.to_string(),
                    total_amount=total.to_string(),
                )
            )
        return CreditCurrent(
            credit_id=credit.id,
            ledger_id=credit.ledger_id,
            document_id=document.id,
            document_kind=document.document_kind,
            party_id=document.party_id,
            asset_id=asset.asset_id,
            version=version,
            status="cancelled" if cancelled else "active",
            revision_id=identifier,
            revision_reason=None if revision is None else revision.revision_reason,
            credit_date=day,
            credit_reason=reason,
            members=members,
        )
    except (MoneyError, ValidationError):
        raise integrity() from None


class BusinessCreditRevisionReads(LedgerService):
    def current(self, owner_id, ledger_id, credit_id):
        """Five service SELECTs, including authorization, regardless of member count."""
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            latest = (
                select(BusinessCreditRevision)
                .where(
                    BusinessCreditRevision.credit_id == BusinessCredit.id,
                    BusinessCreditRevision.ledger_id == BusinessCredit.ledger_id,
                    BusinessCreditRevision.document_id == BusinessCredit.document_id,
                )
                .order_by(BusinessCreditRevision.group_version.desc())
                .limit(1)
                .correlate(BusinessCredit)
                .lateral("latest_credit_revision")
            )
            revision = aliased(BusinessCreditRevision, latest)
            saved = session.execute(
                select(BusinessCredit, BusinessDocument, AssetRecord, revision)
                .join(
                    BusinessDocument,
                    (BusinessDocument.id == BusinessCredit.document_id)
                    & (BusinessDocument.ledger_id == BusinessCredit.ledger_id),
                )
                .join(AssetRecord, AssetRecord.asset_id == BusinessDocument.asset_id)
                .outerjoin(revision, true())
                .where(BusinessCredit.id == credit_id, BusinessCredit.ledger_id == ledger_id)
            ).one_or_none()
            if saved is None:
                raise _not_found()
            credit, document, record, revision = saved
            facts = posting_facts(ledger_id, credit_ids=[credit_id])
            rows = (
                session.execute(
                    select(facts)
                    .where(
                        facts.c.group_version == (1 if revision is None else revision.group_version)
                    )
                    .order_by(facts.c.line_no)
                    .limit(201)
                )
                .mappings()
                .all()
            )
            return _current(credit, document, asset_definition(record), revision, rows)
