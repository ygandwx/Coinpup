"""Structure input invariants independent of PostgreSQL."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetCreate,
    AssetUpdate,
    CategoryCreate,
    CategoryUpdate,
    EntityCreate,
    EntityProfile,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.ledger.templates import TEMPLATES, get_templates
from pydantic import ValidationError


@pytest.mark.parametrize(
    "country,region,company_type",
    [
        ("CN", None, "limited_liability"),
        ("HK", None, "private_limited"),
        ("US", "NM", "llc"),
        ("US", "WY", "llc"),
        ("EE", None, "private_limited"),
    ],
)
def test_confirmed_company_profiles(country, region, company_type):
    profile = EntityCreate(
        kind="company",
        name="  Fictional company  ",
        base_asset_id="USD",
        country_code=country,
        region_code=region,
        company_type=company_type,
        details={"registration_identifier": "fictional-example"},
    )
    assert profile.name == "Fictional company"
    assert profile.details == {"registration_identifier": "fictional-example"}


@pytest.mark.parametrize(
    "fields",
    [
        {"country_code": "US", "region_code": "CA", "company_type": "llc"},
        {"country_code": "US", "company_type": "llc"},
        {"country_code": "CN", "company_type": "private_limited"},
        {"country_code": "HK", "region_code": "NM", "company_type": "private_limited"},
        {"country_code": "DE", "company_type": "private_limited"},
        {},
    ],
)
def test_inconsistent_company_profiles_rejected(fields):
    with pytest.raises(ValidationError):
        EntityProfile(kind="company", name="Fictional company", **fields)


def test_personal_company_fields_rejected_and_client_ids_preserved():
    with pytest.raises(ValidationError):
        EntityCreate(kind="personal", name="Person", base_asset_id="USD", country_code="US")
    entity_id, ledger_id = uuid4(), uuid4()
    entity = EntityCreate(
        id=str(entity_id),
        ledger_id=str(ledger_id),
        kind="personal",
        name="Person",
        base_asset_id="USD",
        template_key="personal_default",
        locale="en",
    )
    assert (entity.id, entity.ledger_id) == (entity_id, ledger_id)


@pytest.mark.parametrize(
    "date_value", [0, 1735689600, "1735689600", "2026-01-01T00:00:00Z", "2026-02-30", "2026-1-1"]
)
def test_registration_date_rejects_timestamp_coercion_and_invalid_calendar_dates(date_value):
    with pytest.raises(ValidationError):
        EntityCreate(
            kind="company",
            name="Fictional company",
            base_asset_id="USD",
            country_code="HK",
            company_type="private_limited",
            registration_date=date_value,
        )


@pytest.mark.parametrize(
    "details",
    [
        {"": "value"},
        {" ": "value"},
        {"key": 12},
        {"key": {"nested": "data"}},
        {"key": "v" * 2001},
        {"k" * 65: "value"},
        {str(index): "value" for index in range(33)},
        {"key\n": "value"},
        {"key": "invalid\x00value"},
    ],
)
def test_profile_details_are_bounded_string_dicts(details):
    with pytest.raises(ValidationError):
        EntityCreate(kind="personal", name="Person", base_asset_id="USD", details=details)


@pytest.mark.parametrize("name", ["", "   ", "line\nbreak", "a" * 161, 123])
def test_names_are_bounded_nonempty_text(name):
    with pytest.raises(ValidationError):
        CategoryCreate(name=name, kind="expense")


@pytest.mark.parametrize("asset_ids", [[], ["USD", "USD"], [""], [1], ["USD"] * 101])
def test_account_asset_list_must_be_bounded_unique_and_nonempty(asset_ids):
    with pytest.raises(ValidationError):
        AccountCreate(name="Account", kind="bank", asset_ids=asset_ids)


@pytest.mark.parametrize(
    "cls,fields",
    [
        (EntityUpdate, {"kind": "company"}),
        (EntityUpdate, {"owner_id": str(uuid4())}),
        (EntityUpdate, {"base_asset_id": "GBP"}),
        (CategoryUpdate, {"parent_id": str(uuid4())}),
        (CategoryUpdate, {"kind": "income"}),
        (AssetUpdate, {"enabled": True, "scale": 6}),
        (AccountUpdate, {"balance": "10.00"}),
        (AccountUpdate, {"ledger_id": str(uuid4())}),
    ],
)
def test_identity_balance_and_unsupported_mutations_rejected(cls, fields):
    with pytest.raises(ValidationError):
        cls(expected_version=1, **fields)


@pytest.mark.parametrize("fields", [{}, {"name": None}, {"archived": None}, {"details": None}])
def test_updates_need_change_and_cannot_null_required_values(fields):
    with pytest.raises(ValidationError):
        EntityUpdate(expected_version=1, **fields)


@pytest.mark.parametrize("version", [0, -1, True, 1.0, "1"])
def test_versions_are_positive_strict_integers(version):
    with pytest.raises(ValidationError):
        EntityUpdate(expected_version=version, name="Changed")


def test_explicit_null_clears_optional_values_but_absence_preserves_them():
    clear = CategoryUpdate(expected_version=1, name_en=None)
    rename = CategoryUpdate(expected_version=1, name="Changed")
    assert clear.model_dump(exclude_unset=True) == {"expected_version": 1, "name_en": None}
    assert "name_en" not in rename.model_dump(exclude_unset=True)


def test_templates_are_immutable_bilingual_and_keys_independent_of_locale():
    assert len(get_templates()) == 2
    for template in get_templates():
        assert template.name and template.name_en
        assert len({item.key for item in template.categories}) == len(template.categories)
        with pytest.raises(ValidationError):
            template.categories[0].name = "Changed"
    with pytest.raises(TypeError):
        TEMPLATES["personal_default"] = None


def test_service_without_storage_fails_only_when_called():
    service = LedgerService(None)
    with pytest.raises(LedgerError) as error:
        service.list_entities(uuid4())
    assert (error.value.code, error.value.status) == ("ledger_unavailable", 503)


@pytest.mark.parametrize("limit,offset", [(0, 0), (201, 0), (True, 0), (1, -1), (1, True)])
def test_service_pagination_bounds_before_database(limit, offset):
    with pytest.raises(LedgerError) as error:
        LedgerService(None).list_assets(uuid4(), limit=limit, offset=offset)
    assert error.value.code == "invalid_pagination"


@pytest.mark.parametrize("scale", [True, 1.0, "6", -1, 19])
def test_asset_scale_does_not_coerce_lossy_or_ambiguous_types(scale):
    with pytest.raises(ValidationError):
        AssetCreate(code="USDC", kind="token", network="test", token_reference="Fake", scale=scale)
