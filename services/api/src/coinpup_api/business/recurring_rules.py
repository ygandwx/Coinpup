"""Captured invoice templates and explicit pause/restore; no scheduler or posting."""

from sqlalchemy import select, text

from coinpup_api.business.draft_schemas import BusinessDraftCreate, BusinessDraftLineInput
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.models import RecurringInvoiceRule as Rule
from coinpup_api.business.recurring_schemas import RecurringRuleResponse
from coinpup_api.ledger.service import LedgerError, _not_found, _page, _touch, _version


class RecurringRuleService(DraftService):
    @staticmethod
    def _rule(session, ledger_id, identifier):
        row = session.scalar(select(Rule).where(Rule.id == identifier, Rule.ledger_id == ledger_id))
        if row is None:
            raise _not_found()
        return row

    def _capture(self, session, ledger_id, entity, identifier, version):
        document = self._draft(session, ledger_id, identifier)
        _version(document, version)
        if document.document_kind != "invoice" or document.archived:
            raise LedgerError("recurrence_template", 409, "Select an active invoice draft.")
        lines = self._lines(session, ledger_id, identifier)
        template = BusinessDraftCreate(
            **{
                field: getattr(document, field)
                for field in BusinessDraftCreate.model_fields
                if field != "lines"
            },
            lines=[
                {field: getattr(line, field) for field in BusinessDraftLineInput.model_fields}
                for line in lines
            ],
        )
        # Same sorted asset lock and current reference/pricing checks as manual drafts.
        self._prepare(session, ledger_id, entity, template)
        return template.model_dump(mode="json")

    def create_rule(self, owner_id, ledger_id, payload):
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            if not session.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name=:name)"),
                {"name": payload.timezone_name},
            ):
                raise LedgerError("recurrence_timezone", 422, "Select an available timezone.")
            template = self._capture(
                session, ledger_id, entity, payload.source_document_id, payload.source_version
            )
            row = Rule(ledger_id=ledger_id, template_input=template, **payload.model_dump())
            session.add(row)
            session.flush()
            return RecurringRuleResponse.model_validate(row)

    def update_rule(self, owner_id, ledger_id, identifier, payload):
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._ledger(session, owner_id, ledger_id, write=True)
            row = self._rule(session, ledger_id, identifier)
            _version(row, payload.expected_version)
            if row.archived:
                raise LedgerError("recurrence_archived", 409, "Restore the rule before editing.")
            if payload.source_document_id is not None:
                template = self._capture(
                    session, ledger_id, entity, payload.source_document_id, payload.source_version
                )
                row.source_document_id = payload.source_document_id
                row.source_version = payload.source_version
                row.template_input = template
            row.name = payload.name
            _touch(row)
            session.flush()
            session.refresh(row)
            return RecurringRuleResponse.model_validate(row)

    def archive_rule(self, owner_id, ledger_id, identifier, payload):
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            row = self._rule(session, ledger_id, identifier)
            _version(row, payload.expected_version)
            # Pause/restore cannot replace the template or silently skip overdue instances.
            row.archived = payload.archived
            _touch(row)
            session.flush()
            session.refresh(row)
            return RecurringRuleResponse.model_validate(row)

    def get_rule(self, owner_id, ledger_id, identifier):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return RecurringRuleResponse.model_validate(self._rule(session, ledger_id, identifier))

    def list_rules(self, owner_id, ledger_id, *, include_archived=True, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(Rule).where(Rule.ledger_id == ledger_id)
            if not include_archived:
                statement = statement.where(Rule.archived.is_(False))
            return [
                RecurringRuleResponse.model_validate(row)
                for row in session.scalars(
                    statement.order_by(Rule.created_at, Rule.id).limit(limit).offset(offset)
                )
            ]
