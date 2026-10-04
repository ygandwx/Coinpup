"""Transactional, owner-scoped structure management; no balances or postings yet."""

from contextlib import contextmanager
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, Category, Entity, Ledger
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    AssetCreate,
    AssetResponse,
    AssetUpdate,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    EntityCreate,
    EntityProfile,
    EntityResponse,
    EntityUpdate,
    LedgerResponse,
    TemplateResponse,
)
from coinpup_api.ledger.templates import TEMPLATES, get_templates
from coinpup_api.models import Administrator
from coinpup_api.sync.locking import acquire_write_lock


class LedgerError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status


def _not_found():
    return LedgerError("not_found", 404, "The requested record was not found.")


def _version(record, expected):
    if record.version != expected:
        raise LedgerError("version_conflict", 409, "The record changed; reload before editing.")


def _touch(record):
    record.version += 1
    record.updated_at = func.clock_timestamp()


def _page(limit, offset):
    if (
        type(limit) is not int
        or not 1 <= limit <= 200
        or type(offset) is not int
        or not 0 <= offset <= 100000
    ):
        raise LedgerError("invalid_pagination", 422, "Pagination is outside the allowed bounds.")


class LedgerService:
    def __init__(self, engine: Engine | None):
        self.engine = engine

    def require_engine(self) -> Engine:
        if self.engine is None:
            raise LedgerError("ledger_unavailable", 503, "Ledger storage is unavailable.")
        return self.engine

    @contextmanager
    def _transaction(self, owner_id, *, read_only=False, write=False):
        try:
            with Session(self.require_engine()) as session, session.begin():
                if read_only:
                    # Configure this transaction before the owner lookup establishes its snapshot.
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                if write:
                    # Order cursor allocation before the owner lookup or any business row lock.
                    acquire_write_lock(session)
                # A service caller cannot bypass authentication by providing an unknown owner.
                if session.get(Administrator, owner_id) is None:
                    raise _not_found()
                yield session
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise LedgerError("duplicate_record", 409, "The record already exists.") from None
            raise LedgerError(
                "constraint_conflict", 409, "The change conflicts with an existing record."
            ) from None

    def _ledger(self, session, owner_id, ledger_id, *, write=False):
        statement = select(Ledger).where(Ledger.id == ledger_id, Ledger.owner_id == owner_id)
        if write:
            statement = statement.with_for_update()
        ledger = session.scalar(statement)
        if ledger is None:
            raise _not_found()
        entity_statement = select(Entity).where(
            Entity.id == ledger.entity_id, Entity.owner_id == owner_id
        )
        if write:
            entity_statement = entity_statement.with_for_update()
        entity = session.scalar(entity_statement)
        if entity is None:
            raise _not_found()
        if write and entity.archived:
            raise LedgerError(
                "entity_archived", 409, "Restore the entity before changing its ledger."
            )
        return ledger, entity

    def _assets(self, session, asset_ids):
        # Shared locks serialize against catalog disable without serializing independent users.
        records = session.scalars(
            select(AssetRecord)
            .where(AssetRecord.asset_id.in_(asset_ids))
            .order_by(AssetRecord.asset_id)
            .with_for_update(read=True)
        ).all()
        if len(records) != len(asset_ids):
            raise _not_found()
        if any(not record.enabled for record in records):
            raise LedgerError("asset_disabled", 409, "An asset is disabled for new use.")
        return records

    @staticmethod
    def _entity_response(entity, ledger):
        data = {key: getattr(entity, key) for key in EntityResponse.model_fields if key != "ledger"}
        return EntityResponse(**data, ledger=LedgerResponse.model_validate(ledger))

    @staticmethod
    def _account_response(session, account):
        data = {
            key: getattr(account, key) for key in AccountResponse.model_fields if key != "asset_ids"
        }
        asset_ids = session.scalars(
            select(AccountAsset.asset_id)
            .where(AccountAsset.account_id == account.id, AccountAsset.enabled.is_(True))
            .order_by(AccountAsset.asset_id)
        ).all()
        return AccountResponse(**data, asset_ids=asset_ids)

    def list_assets(self, owner_id, include_disabled=True, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            statement = select(AssetRecord)
            if not include_disabled:
                statement = statement.where(AssetRecord.enabled.is_(True))
            return [
                AssetResponse.model_validate(item)
                for item in session.scalars(
                    statement.order_by(AssetRecord.asset_id).limit(limit).offset(offset)
                )
            ]

    def create_asset(self, owner_id, payload: AssetCreate) -> AssetResponse:
        try:
            definition = AssetDefinition(**payload.model_dump())
        except MoneyError as error:
            raise LedgerError(error.code, 422, "Asset identity or precision is invalid.") from None
        with self._transaction(owner_id, write=True) as session:
            record = AssetRecord(asset_id=definition.asset_id, **payload.model_dump())
            session.add(record)
            session.flush()
            return AssetResponse.model_validate(record)

    def update_asset(self, owner_id, asset_id, payload: AssetUpdate) -> AssetResponse:
        with self._transaction(owner_id, write=True) as session:
            record = session.scalar(
                select(AssetRecord).where(AssetRecord.asset_id == asset_id).with_for_update()
            )
            if record is None:
                raise _not_found()
            _version(record, payload.expected_version)
            record.enabled = payload.enabled
            _touch(record)
            session.flush()
            session.refresh(record)
            return AssetResponse.model_validate(record)

    def list_templates(self, owner_id) -> list[TemplateResponse]:
        with self._transaction(owner_id):
            return get_templates()

    def list_entities(self, owner_id, include_archived=False, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            statement = (
                select(Entity, Ledger)
                .join(Ledger, Ledger.entity_id == Entity.id)
                .where(Entity.owner_id == owner_id, Ledger.owner_id == owner_id)
            )
            if not include_archived:
                statement = statement.where(Entity.archived.is_(False))
            return [
                self._entity_response(entity, ledger)
                for entity, ledger in session.execute(
                    statement.order_by(Entity.id).limit(limit).offset(offset)
                )
            ]

    def get_entity(self, owner_id, entity_id) -> EntityResponse:
        with self._transaction(owner_id) as session:
            row = session.execute(
                select(Entity, Ledger)
                .join(Ledger, Ledger.entity_id == Entity.id)
                .where(
                    Entity.id == entity_id, Entity.owner_id == owner_id, Ledger.owner_id == owner_id
                )
            ).first()
            if row is None:
                raise _not_found()
            return self._entity_response(*row)

    def get_ledger(self, owner_id, ledger_id) -> LedgerResponse:
        with self._transaction(owner_id) as session:
            ledger, _ = self._ledger(session, owner_id, ledger_id)
            return LedgerResponse.model_validate(ledger)

    def create_entity(self, owner_id, payload: EntityCreate) -> EntityResponse:
        with self._transaction(owner_id, write=True) as session:
            asset = self._assets(session, [payload.base_asset_id])[0]
            if asset.kind != "fiat":
                raise LedgerError("invalid_base_asset", 422, "A ledger base asset must be fiat.")
            entity = Entity(
                id=payload.id or uuid4(),
                owner_id=owner_id,
                **payload.model_dump(
                    exclude={"id", "ledger_id", "base_asset_id", "template_key", "locale"}
                ),
            )
            session.add(entity)
            session.flush()
            ledger = Ledger(
                id=payload.ledger_id or uuid4(),
                entity_id=entity.id,
                owner_id=owner_id,
                base_asset_id=payload.base_asset_id,
            )
            session.add(ledger)
            session.flush()
            if payload.template_key:
                template = TEMPLATES[payload.template_key]
                for item in template.categories:
                    session.add(
                        Category(
                            id=uuid4(),
                            ledger_id=ledger.id,
                            name=item.name if payload.locale == "zh" else item.name_en,
                            name_en=item.name_en,
                            kind=item.kind,
                            template_key=f"{template.key}:{item.key}",
                        )
                    )
                session.flush()
            return self._entity_response(entity, ledger)

    def update_entity(self, owner_id, entity_id, payload: EntityUpdate) -> EntityResponse:
        with self._transaction(owner_id, write=True) as session:
            # Every ledger mutation acquires ledger then entity, including archive/unarchive.
            ledger = session.scalar(
                select(Ledger)
                .where(Ledger.entity_id == entity_id, Ledger.owner_id == owner_id)
                .with_for_update()
            )
            if ledger is None:
                raise _not_found()
            entity = session.scalar(
                select(Entity)
                .where(Entity.id == entity_id, Entity.owner_id == owner_id)
                .with_for_update()
            )
            if entity is None:
                raise _not_found()
            _version(entity, payload.expected_version)
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
            profile = {field: getattr(entity, field) for field in EntityProfile.model_fields}
            profile.update({key: value for key, value in changes.items() if key != "archived"})
            try:
                EntityProfile.model_validate(profile)
            except ValidationError:
                raise LedgerError(
                    "invalid_profile", 422, "Company profile fields are inconsistent."
                ) from None
            for key, value in changes.items():
                setattr(entity, key, value)
            _touch(entity)
            session.flush()
            session.refresh(entity)
            return self._entity_response(entity, ledger)

    def list_accounts(self, owner_id, ledger_id, include_archived=False, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(Account).where(Account.ledger_id == ledger_id)
            if not include_archived:
                statement = statement.where(Account.archived.is_(False))
            return [
                self._account_response(session, item)
                for item in session.scalars(
                    statement.order_by(Account.id).limit(limit).offset(offset)
                )
            ]

    def _set_account_assets(self, session, account, asset_ids):
        self._assets(session, asset_ids)
        existing = {
            row.asset_id: row
            for row in session.scalars(
                select(AccountAsset).where(AccountAsset.account_id == account.id)
            )
        }
        for asset_id, link in existing.items():
            link.enabled = asset_id in asset_ids
        for asset_id in asset_ids:
            if asset_id not in existing:
                session.add(
                    AccountAsset(
                        account_id=account.id, ledger_id=account.ledger_id, asset_id=asset_id
                    )
                )

    def create_account(self, owner_id, ledger_id, payload: AccountCreate) -> AccountResponse:
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            account = Account(
                id=payload.id or uuid4(),
                ledger_id=ledger_id,
                **payload.model_dump(exclude={"id", "asset_ids"}),
            )
            session.add(account)
            session.flush()
            self._set_account_assets(session, account, payload.asset_ids)
            session.flush()
            return self._account_response(session, account)

    def update_account(self, owner_id, ledger_id, account_id, payload: AccountUpdate):
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            account = session.scalar(
                select(Account).where(Account.id == account_id, Account.ledger_id == ledger_id)
            )
            if account is None:
                raise _not_found()
            _version(account, payload.expected_version)
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
            asset_ids = changes.pop("asset_ids", None)
            for key, value in changes.items():
                setattr(account, key, value)
            if asset_ids is not None:
                self._set_account_assets(session, account, asset_ids)
            _touch(account)
            session.flush()
            session.refresh(account)
            return self._account_response(session, account)

    def list_categories(self, owner_id, ledger_id, include_archived=False, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(Category).where(Category.ledger_id == ledger_id)
            if not include_archived:
                statement = statement.where(Category.archived.is_(False))
            return [
                CategoryResponse.model_validate(item)
                for item in session.scalars(
                    statement.order_by(Category.id).limit(limit).offset(offset)
                )
            ]

    @staticmethod
    def _parent(session, ledger_id, parent_id, kind):
        if parent_id is None:
            return
        parent = session.scalar(
            select(Category).where(Category.id == parent_id, Category.ledger_id == ledger_id)
        )
        if parent is None:
            raise _not_found()
        if parent.kind != kind:
            raise LedgerError("category_kind_mismatch", 422, "Parent and child kinds must match.")
        if parent.archived:
            raise LedgerError("parent_archived", 409, "Restore the parent category before use.")

    def create_category(self, owner_id, ledger_id, payload: CategoryCreate) -> CategoryResponse:
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            self._parent(session, ledger_id, payload.parent_id, payload.kind)
            category = Category(
                id=payload.id or uuid4(), ledger_id=ledger_id, **payload.model_dump(exclude={"id"})
            )
            session.add(category)
            session.flush()
            return CategoryResponse.model_validate(category)

    def update_category(self, owner_id, ledger_id, category_id, payload: CategoryUpdate):
        with self._transaction(owner_id, write=True) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            category = session.scalar(
                select(Category).where(Category.id == category_id, Category.ledger_id == ledger_id)
            )
            if category is None:
                raise _not_found()
            _version(category, payload.expected_version)
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
            if changes.get("archived") is True:
                if session.scalar(
                    select(Category.id)
                    .where(
                        Category.ledger_id == ledger_id,
                        Category.parent_id == category.id,
                        Category.archived.is_(False),
                    )
                    .limit(1)
                ):
                    raise LedgerError("active_children", 409, "Archive child categories first.")
            elif changes.get("archived") is False:
                self._parent(session, ledger_id, category.parent_id, category.kind)
            for key, value in changes.items():
                setattr(category, key, value)
            _touch(category)
            session.flush()
            session.refresh(category)
            return CategoryResponse.model_validate(category)
