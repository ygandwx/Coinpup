"""Owned master data writes share the existing advisory/ledger/entity lock boundary."""

from sqlalchemy import select

from coinpup_api.business.models import BusinessParty, BusinessProject
from coinpup_api.business.schemas import (
    PartyCreate,
    PartyResponse,
    PartyUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
)
from coinpup_api.ledger.service import (
    LedgerError,
    LedgerService,
    _not_found,
    _page,
    _touch,
    _version,
)


class BusinessService(LedgerService):
    def _read(self, owner_id, ledger_id, model, response, identifier):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            row = session.scalar(
                select(model).where(model.id == identifier, model.ledger_id == ledger_id)
            )
            if row is None:
                raise _not_found()
            return response.model_validate(row)

    def _list(
        self, owner_id, ledger_id, model, response, *, include_archived=True, limit=100, offset=0
    ):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(model).where(model.ledger_id == ledger_id)
            if not include_archived:
                statement = statement.where(model.archived.is_(False))
            rows = session.scalars(
                statement.order_by(model.created_at, model.id).limit(limit).offset(offset)
            )
            return [response.model_validate(row) for row in rows]

    def _create(self, owner_id, ledger_id, model, response, payload):
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            row = model(ledger_id=ledger_id, **payload.model_dump())
            session.add(row)
            session.flush()
            return response.model_validate(row)

    def _update(self, owner_id, ledger_id, identifier, model, response, payload):
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            row = session.scalar(
                select(model).where(model.id == identifier, model.ledger_id == ledger_id)
            )
            if row is None:
                raise _not_found()
            _version(row, payload.expected_version)
            for field, value in payload.model_dump(
                exclude_unset=True, exclude={"expected_version"}
            ).items():
                setattr(row, field, value)
            if model is BusinessParty:
                anonymous = all(
                    getattr(row, field) is None
                    for field in (
                        "name",
                        "role",
                        "legal_name",
                        "email",
                        "phone",
                        "address",
                        "tax_identifier",
                        "notes",
                    )
                )
                if not anonymous and (row.name is None or row.role is None):
                    raise LedgerError(
                        "party_profile_incomplete",
                        422,
                        "Complete the party name and role together.",
                    )
            _touch(row)
            session.flush()
            session.refresh(row)
            return response.model_validate(row)

    def list_parties(self, owner_id, ledger_id, **page):
        return self._list(owner_id, ledger_id, BusinessParty, PartyResponse, **page)

    def get_party(self, owner_id, ledger_id, identifier):
        return self._read(owner_id, ledger_id, BusinessParty, PartyResponse, identifier)

    def create_party(self, owner_id, ledger_id, payload: PartyCreate):
        return self._create(owner_id, ledger_id, BusinessParty, PartyResponse, payload)

    def update_party(self, owner_id, ledger_id, identifier, payload: PartyUpdate):
        return self._update(owner_id, ledger_id, identifier, BusinessParty, PartyResponse, payload)

    def list_projects(self, owner_id, ledger_id, **page):
        return self._list(owner_id, ledger_id, BusinessProject, ProjectResponse, **page)

    def get_project(self, owner_id, ledger_id, identifier):
        return self._read(owner_id, ledger_id, BusinessProject, ProjectResponse, identifier)

    def create_project(self, owner_id, ledger_id, payload: ProjectCreate):
        return self._create(owner_id, ledger_id, BusinessProject, ProjectResponse, payload)

    def update_project(self, owner_id, ledger_id, identifier, payload: ProjectUpdate):
        return self._update(
            owner_id, ledger_id, identifier, BusinessProject, ProjectResponse, payload
        )
