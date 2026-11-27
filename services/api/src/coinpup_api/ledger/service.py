"""Transactional, owner-scoped structure management; no balances or postings yet."""

from contextlib import contextmanager
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coinpup_api.ledger.assets import AssetDefinition, verified_token
from coinpup_api.ledger.card_details import (
    CARD_KINDS,
    public_account_details,
    valid_account_details,
    valid_card_number,
)
from coinpup_api.ledger.crypto_profiles import (
    CRYPTO_PRESETS,
    PLATFORM_MODES,
    custom_platform_key,
    names_known_platform,
    supports_deposit_network,
    valid_deposit_address,
    valid_deposit_memo,
)
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, Category, Entity, Ledger
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    AssetCreate,
    AssetResponse,
    AssetUpdate,
    CardNumberResponse,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    CryptoAssetDetails,
    CryptoAssetProfile,
    CryptoPlatformProfile,
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


def _invalid_card_details():
    return LedgerError(
        "invalid_card_details",
        422,
        "Use the designated card fields and omit card security codes or PINs.",
    )


def _validate_card_details(kind: str, details: dict[str, str], previous: dict[str, str]):
    if not valid_account_details(kind, details, previous):
        raise _invalid_card_details()


def _card_number(kind: str, secret):
    if secret is None:
        return None
    number = secret.get_secret_value()
    if kind not in CARD_KINDS or not valid_card_number(number):
        raise _invalid_card_details()
    return number


def _check_custody(kind, mode, asset_kinds):
    if mode is not None and kind != "crypto":
        raise LedgerError("invalid_custody_mode", 422, "Only crypto accounts have a custody mode.")
    allowed = {
        None: {"fiat", "native", "token"},
        "onchain": {"native", "token"},
        "custodial": {"custodial"},
    }[mode]
    if not allowed.issuperset(asset_kinds):
        raise LedgerError(
            "custody_asset_mismatch", 422, "Account assets do not match the selected custody mode."
        )


def _invalid_crypto_platform():
    return LedgerError("invalid_crypto_platform", 422, "Choose a platform for this custody mode.")


def _invalid_crypto_assets():
    return LedgerError(
        "invalid_crypto_asset_details", 422, "Check the selected assets and their network details."
    )


class LedgerService:
    def __init__(self, engine: Engine | None):
        self.engine = engine

    def require_engine(self) -> Engine:
        if self.engine is None:
            raise LedgerError("ledger_unavailable", 503, "Ledger storage is unavailable.")
        return self.engine

    @contextmanager
    def _transaction(self, owner_id, *, read_only=False, write=False, read_committed=False):
        try:
            with Session(self.require_engine()) as session, session.begin():
                if read_only:
                    # Configure this transaction before the owner lookup establishes its snapshot.
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                elif read_committed:
                    # These writes need a fresh snapshot after waiting for the lock.
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                if write:
                    # Order cursor allocation before the owner lookup or any business row lock.
                    acquire_write_lock(session)
                # A service caller cannot bypass authentication by providing an unknown owner.
                if session.get(Administrator, owner_id) is None:
                    raise _not_found()
                yield session
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                if (
                    getattr(getattr(error.orig, "diag", None), "constraint_name", None)
                    == "uq_accounts_crypto_platform_ledger"
                ):
                    raise LedgerError(
                        "duplicate_crypto_platform", 409, "This ledger already has that platform."
                    ) from None
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
    def _account_response(session, account, card_summary_only=False):
        data = {
            key: getattr(account, key)
            for key in AccountResponse.model_fields
            if key
            not in {"asset_ids", "details", "has_card_number", "crypto_platform", "crypto_assets"}
        }
        details = public_account_details(account.kind, account.details)
        if card_summary_only:
            details = {
                key: details[key]
                for key in ("card_last_four", "expiry")
                if key in details and account.kind in CARD_KINDS
            }
        has_card_number = (
            not card_summary_only and account.kind in CARD_KINDS and account.has_card_number
        )
        links = (
            AccountAsset.account_id == account.id,
            AccountAsset.ledger_id == account.ledger_id,
        )
        platform, asset_profiles = None, []
        if card_summary_only:
            asset_ids = session.scalars(
                select(AccountAsset.asset_id)
                .where(*links, AccountAsset.enabled.is_(True))
                .order_by(AccountAsset.asset_id)
            ).all()
        else:
            rows = session.scalars(
                select(AccountAsset).where(*links).order_by(AccountAsset.asset_id)
            ).all()
            asset_ids = [row.asset_id for row in rows if row.enabled]
            if account.crypto_platform_key is not None:
                platform = CryptoPlatformProfile(
                    institution_id=account.crypto_platform_id,
                    custom_name=account.crypto_platform_name,
                    uid=account.crypto_platform_uid,
                    email=account.crypto_platform_email,
                    note=account.crypto_platform_note,
                )
            asset_profiles = [
                CryptoAssetProfile(
                    asset_id=row.asset_id,
                    enabled=row.enabled,
                    details=CryptoAssetDetails(
                        network=row.crypto_network,
                        address=row.crypto_address,
                        memo=row.crypto_memo,
                        label=row.crypto_label,
                    ),
                )
                for row in rows
                if any((row.crypto_network, row.crypto_address, row.crypto_memo, row.crypto_label))
            ]
        return AccountResponse(
            **data,
            details=details,
            asset_ids=asset_ids,
            has_card_number=has_card_number,
            crypto_platform=platform,
            crypto_assets=asset_profiles,
        )

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
        if definition.kind == "token":
            official = verified_token(definition.network, definition.token_reference)
            if official is not None and (definition.code, definition.scale) != (
                official.code,
                official.scale,
            ):
                raise LedgerError(
                    "asset_definition", 422, "Use the verified code and precision for this token."
                )
        with self._transaction(owner_id, write=True, read_committed=True) as session:
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

    def list_accounts(
        self,
        owner_id,
        ledger_id,
        include_archived=False,
        limit=100,
        offset=0,
        *,
        card_summary_only=False,
    ):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(Account).where(
                Account.ledger_id == ledger_id, Account.account_class == "money"
            )
            if not include_archived:
                statement = statement.where(Account.archived.is_(False))
            return [
                self._account_response(session, item, card_summary_only)
                for item in session.scalars(
                    statement.order_by(Account.id).limit(limit).offset(offset)
                )
            ]

    def _set_account_assets(self, session, account, asset_ids):
        assets = self._assets(session, asset_ids)
        _check_custody(account.kind, account.custody_mode, {asset.kind for asset in assets})
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

    @staticmethod
    def _register_crypto_presets(session, kind, mode, asset_ids, preset_ids):
        if not preset_ids:
            return
        if kind != "crypto" or mode is None or not set(preset_ids).issubset(asset_ids or ()):
            raise _invalid_crypto_assets()
        if any(asset_id not in CRYPTO_PRESETS for asset_id in preset_ids):
            raise _invalid_crypto_assets()
        presets = [CRYPTO_PRESETS[asset_id] for asset_id in sorted(preset_ids)]
        _check_custody(kind, mode, {asset.kind for asset in presets})
        existing = {
            item.asset_id: item
            for item in session.scalars(
                select(AssetRecord).where(AssetRecord.asset_id.in_(preset_ids))
            )
        }
        for preset in presets:
            saved = existing.get(preset.asset_id)
            if saved is not None:
                if (saved.code, saved.kind, saved.scale, saved.network, saved.token_reference) != (
                    preset.code,
                    preset.kind,
                    preset.scale,
                    preset.network,
                    preset.token_reference,
                ):
                    raise LedgerError(
                        "constraint_conflict", 409, "The preset conflicts with a saved asset."
                    )
                continue
            session.add(
                AssetRecord(
                    asset_id=preset.asset_id,
                    code=preset.code,
                    kind=preset.kind,
                    scale=preset.scale,
                    network=preset.network,
                    token_reference=preset.token_reference,
                )
            )
        session.flush()

    @staticmethod
    def _set_crypto_platform(account, profile):
        if profile is None:
            account.crypto_platform_key = None
            account.crypto_platform_id = None
            account.crypto_platform_name = None
            account.crypto_platform_uid = None
            account.crypto_platform_email = None
            account.crypto_platform_note = None
            return
        if account.kind != "crypto" or account.custody_mode is None:
            raise _invalid_crypto_platform()
        institution = profile.institution_id
        if institution is not None and PLATFORM_MODES.get(institution) != account.custody_mode:
            raise _invalid_crypto_platform()
        if profile.custom_name is not None and names_known_platform(profile.custom_name):
            raise _invalid_crypto_platform()
        account.crypto_platform_key = (
            f"catalog:{institution}"
            if institution is not None
            else custom_platform_key(profile.custom_name)
        )
        account.crypto_platform_id = institution
        account.crypto_platform_name = profile.custom_name
        account.crypto_platform_uid = profile.uid
        account.crypto_platform_email = profile.email
        account.crypto_platform_note = profile.note
        account.crypto_profile_used = True

    @staticmethod
    def _set_crypto_asset_profiles(session, account, changes):
        if not changes:
            return
        if account.kind != "crypto" or account.custody_mode is None:
            raise _invalid_crypto_assets()
        ids = [change.asset_id for change in changes]
        rows = session.execute(
            select(AccountAsset, AssetRecord)
            .join(AssetRecord, AssetRecord.asset_id == AccountAsset.asset_id)
            .where(
                AccountAsset.account_id == account.id,
                AccountAsset.ledger_id == account.ledger_id,
                AccountAsset.asset_id.in_(ids),
            )
        ).all()
        selected = {row.asset_id: (row, asset) for row, asset in rows}
        if set(selected) != set(ids):
            raise _invalid_crypto_assets()
        for change in changes:
            row, asset = selected[change.asset_id]
            details = change.details
            if details is None:
                row.crypto_network = row.crypto_address = row.crypto_memo = row.crypto_label = None
                continue
            if not row.enabled:
                raise _invalid_crypto_assets()
            network = details.network
            if account.custody_mode == "onchain":
                if network is not None and network != asset.network:
                    raise _invalid_crypto_assets()
                network = asset.network
            elif network is not None and not supports_deposit_network(asset.code, network):
                raise _invalid_crypto_assets()
            if details.address is not None and (
                network is None or not valid_deposit_address(network, details.address)
            ):
                raise _invalid_crypto_assets()
            if details.memo is not None and (
                network is None or not valid_deposit_memo(network, details.address, details.memo)
            ):
                raise _invalid_crypto_assets()
            row.crypto_network = network
            row.crypto_address = details.address
            row.crypto_memo = details.memo
            row.crypto_label = details.label

    def create_account(self, owner_id, ledger_id, payload: AccountCreate) -> AccountResponse:
        with self._transaction(
            owner_id, write=True, read_committed=bool(payload.crypto_preset_asset_ids)
        ) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            details = dict(payload.details)
            _validate_card_details(payload.kind, details, {})
            _check_custody(payload.kind, payload.custody_mode, ())
            number = _card_number(payload.kind, payload.card_number)
            if number is not None:
                details["card_last_four"] = number[-4:]
            self._register_crypto_presets(
                session,
                payload.kind,
                payload.custody_mode,
                payload.asset_ids,
                payload.crypto_preset_asset_ids,
            )
            account = Account(
                id=payload.id or uuid4(),
                ledger_id=ledger_id,
                name=payload.name,
                kind=payload.kind,
                custody_mode=payload.custody_mode,
                details=details,
                card_number=number,
                crypto_profile_used=any(
                    change.details is not None for change in payload.crypto_assets
                ),
            )
            if payload.crypto_platform is not None:
                self._set_crypto_platform(account, payload.crypto_platform)
            session.add(account)
            session.flush()
            self._set_account_assets(session, account, payload.asset_ids)
            self._set_crypto_asset_profiles(session, account, payload.crypto_assets)
            session.flush()
            return self._account_response(session, account)

    def update_account(self, owner_id, ledger_id, account_id, payload: AccountUpdate):
        with self._transaction(
            owner_id, write=True, read_committed=bool(payload.crypto_preset_asset_ids)
        ) as session:
            self._ledger(session, owner_id, ledger_id, write=True)
            account = session.scalar(
                select(Account).where(
                    Account.id == account_id,
                    Account.ledger_id == ledger_id,
                    Account.account_class == "money",
                )
            )
            if account is None:
                raise _not_found()
            _version(account, payload.expected_version)
            changes = payload.model_dump(
                exclude_unset=True,
                exclude={
                    "expected_version",
                    "card_number",
                    "crypto_platform",
                    "crypto_assets",
                    "crypto_preset_asset_ids",
                },
            )
            details = dict(changes.get("details", account.details))
            kind = changes.get("kind", account.kind)
            custody_mode = changes.get("custody_mode", account.custody_mode)
            if kind != account.kind or custody_mode != account.custody_mode:
                asset_kinds = session.scalars(
                    select(AssetRecord.kind)
                    .join(AccountAsset, AccountAsset.asset_id == AssetRecord.asset_id)
                    .where(AccountAsset.account_id == account.id)
                ).all()
                _check_custody(kind, custody_mode, asset_kinds)
            if "details" in changes:
                _validate_card_details(kind, details, account.details)
            if "card_number" in payload.model_fields_set:
                number = _card_number(kind, payload.card_number)
                account.card_number = number
                if number is not None:
                    details["card_last_four"] = number[-4:]
                else:
                    details.pop("card_last_four", None)
                changes["details"] = details
            elif "details" in changes and account.has_card_number:
                # Replacing general details cannot alter a saved card number's suffix.
                details["card_last_four"] = account.details["card_last_four"]
                changes["details"] = details
            asset_ids = changes.pop("asset_ids", None)
            self._register_crypto_presets(
                session, kind, custody_mode, asset_ids, payload.crypto_preset_asset_ids
            )
            for key, value in changes.items():
                setattr(account, key, value)
            if "crypto_platform" in payload.model_fields_set:
                self._set_crypto_platform(account, payload.crypto_platform)
            elif (
                kind == "crypto"
                and account.crypto_platform_key is not None
                and (
                    custody_mode is None
                    or (
                        account.crypto_platform_id is not None
                        and PLATFORM_MODES.get(account.crypto_platform_id) != custody_mode
                    )
                )
            ):
                raise _invalid_crypto_platform()
            if any(change.details is not None for change in payload.crypto_assets):
                account.crypto_profile_used = True
            _touch(account)
            session.flush()
            if asset_ids is not None:
                self._set_account_assets(session, account, asset_ids)
            self._set_crypto_asset_profiles(session, account, payload.crypto_assets)
            session.flush()
            session.refresh(account)
            return self._account_response(session, account)

    def read_card_number(self, owner_id, ledger_id, account_id) -> CardNumberResponse:
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            row = session.execute(
                select(Account.card_number).where(
                    Account.id == account_id,
                    Account.ledger_id == ledger_id,
                    Account.account_class == "money",
                    Account.kind.in_(CARD_KINDS),
                )
            ).one_or_none()
            if row is None:
                raise _not_found()
            return CardNumberResponse(card_number=row.card_number)

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
