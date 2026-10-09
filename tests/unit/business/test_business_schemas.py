"""Business master data boundaries reject ambiguous/unsafe writes before persistence."""

from uuid import uuid4

import pytest
from coinpup_api.business.schemas import PartyCreate, PartyUpdate, ProjectCreate, ProjectUpdate
from pydantic import ValidationError


@pytest.mark.parametrize(
    "model, body",
    [
        (PartyCreate, dict(name="Fictional party", role="customer")),
        (ProjectCreate, dict(name="Fictional project")),
    ],
)
def test_create_requires_stable_identity(model, body):
    with pytest.raises(ValidationError):
        model(**body)
    assert model(id=str(identifier := uuid4()), **body).id == identifier


@pytest.mark.parametrize("model", [PartyUpdate, ProjectUpdate])
@pytest.mark.parametrize(
    "changes",
    [
        {},
        dict(name=None),
        dict(archived=None),
        dict(archived="true"),
        dict(name=123),
        dict(name=" \t"),
        dict(name="Fictional\x01"),
        dict(name="x" * 161),
        dict(notes="x" * 2001),
        dict(notes="Fictional\x00"),
        dict(id=str(uuid4())),
        dict(ledger_id=str(uuid4())),
        dict(balance="100.00"),
    ],
)
def test_update_rejects_empty_unsafe_and_identity_fields(model, changes):
    with pytest.raises(ValidationError):
        model(expected_version=1, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        dict(role=None),
        dict(role="employee"),
        dict(email=123),
        dict(email="Fictional\x00"),
        dict(email="x" * 255),
        dict(phone="x" * 65),
        dict(address="x" * 1001),
        dict(tax_identifier="x" * 129),
        dict(legal_name="x" * 201),
    ],
)
def test_party_contact_bounds_and_role_are_explicit(changes):
    with pytest.raises(ValidationError):
        PartyUpdate(expected_version=1, **changes)


@pytest.mark.parametrize("model", [PartyUpdate, ProjectUpdate])
@pytest.mark.parametrize("version", [True, "1", 0, -1])
def test_version_is_a_positive_strict_integer(model, version):
    with pytest.raises(ValidationError):
        model(expected_version=version, notes="Fictional")


def test_optional_clears_are_explicit_and_bilingual_values_preserved():
    change = PartyUpdate(
        expected_version=1,
        name="  Fictional 示例  ",
        email=None,
        notes="  Fictional 备注\nsecond line  ",
    )
    assert change.name == "Fictional 示例"
    assert change.model_dump(exclude_unset=True) == dict(
        expected_version=1,
        name="Fictional 示例",
        email=None,
        notes="  Fictional 备注\nsecond line  ",
    )
    assert "phone" not in change.model_fields_set
