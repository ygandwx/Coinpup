"""Exact draft line persistence and constraints, using explicitly fictional business data."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine, BusinessProject
from coinpup_api.business.pricing import PriceInput, price_line
from coinpup_api.ledger.assets import get_asset
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_headers import fields, write
from tests.integration.conftest import draft_line_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_business_references import seed as legacy_seed
from tests.integration.ledger.test_journal_dimensions import initial
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_projects import seed as project_seed

pytestmark = pytest.mark.integration


def document(s, asset="USD"):
    return write(s, fields(s) | {"asset_id": asset})


def line(s, parent, value=None, asset="USD", **changes):
    value = value or PriceInput("3", "19.99", "9.97", "8.25")
    price = price_line(value, get_asset(asset))
    return (
        dict(
            id=uuid4(),
            document_id=parent,
            ledger_id=s["ledger"],
            line_no=1,
            description="Fictional service 虚构服务",
            asset_id=asset,
            **asdict(value),
            category_id=s["income"].id,
            category_kind="income",
            project_id=None,
            recognition_date=date(2026, 9, 30),
            category_snapshot={"name": "Fictional income"},
            project_snapshot=None,
            net_amount=price.net.to_decimal(),
            tax_amount=price.tax.to_decimal(),
            total_amount=price.total.to_decimal(),
        )
        | changes
    )


def save(s, *values):
    with s["engine"].begin() as c:
        for value in values:
            c.execute(insert(BusinessDocumentLine).values(**value))


@pytest.mark.parametrize("asset", ["USD", "BTC", "XMR", "ETH"])
def test_database_prices_match_exact_calculator_and_preserve_original_strings(ledger_setup, asset):
    s = ledger_setup
    parent = document(s, asset)
    scale = get_asset(asset).scale
    minimum = "0." + "0" * (scale - 1) + "1"
    inputs = [
        PriceInput("3.00", "19.99", "9.97", "8.2500"),
        PriceInput("0.50", minimum, tax_rate_percent="50"),
        PriceInput("1", minimum, tax_rate_percent="49.999999999999999999"),
        PriceInput("1", minimum, tax_rate_percent="50.000000000000000001"),
    ]
    values = [line(s, parent, value, asset, line_no=i + 1) for i, value in enumerate(inputs)]
    save(s, *values)
    with s["engine"].connect() as c:
        rows = (
            c.execute(select(BusinessDocumentLine.__table__).order_by(BusinessDocumentLine.line_no))
            .mappings()
            .all()
        )
        for row, expected in zip(rows, values, strict=True):
            assert {key: row[key] for key in expected} == expected
        assert rows[2]["tax_amount"] == 0  # No intermediate division rounding near half a unit.
        assert rows[3]["tax_amount"] == Decimal(minimum)
        assert c.execute(
            select(ChangeLog).where(ChangeLog.entity_type == "business_document_lines")
        ).all()


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": "0"},
        {"quantity": "3e0"},
        {"quantity": "03"},
        {"quantity": "3\n"},
        {"quantity": "１"},
        {"unit_price": "19.990"},
        {"unit_price": "-1"},
        {"discount_amount": "99"},
        {"discount_amount": "-0.01"},
        {"tax_rate_percent": "-1"},
        {"tax_rate_percent": "8.25e0"},
        {"net_amount": Decimal("50.01")},
        {"tax_amount": Decimal("4.14")},
        {"total_amount": Decimal("54.14")},
        {"total_amount": Decimal("NaN")},
    ],
)
def test_invalid_source_or_forged_total_rolls_back_all_tables(ledger_setup, change):
    s = ledger_setup
    values = line(s, document(s), **change)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        save(s, values)
    assert failure.value.orig.diag.constraint_name == "ck_business_line_pricing"
    assert snapshot(s) == before


@pytest.mark.parametrize("field", ["quantity", "line_no", "category_snapshot", "recognition_date"])
def test_partial_profiles_are_rejected(ledger_setup, field):
    s = ledger_setup
    values = line(s, document(s), **{field: None})
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        save(s, values)
    assert failure.value.orig.diag.constraint_name == "ck_business_document_lines_profile"
    assert snapshot(s) == before


@pytest.mark.parametrize("reference", ["category", "project", "document"])
def test_foreign_ledger_references_cannot_be_used(ledger_setup, reference):
    s = ledger_setup
    parent = document(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional foreign line",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    foreign = s | {"ledger": other.ledger.id}
    if reference == "category":
        changes = {
            "category_id": next(
                x.id
                for x in s["structure"].list_categories(s["owner"], other.ledger.id)
                if x.kind == "income"
            )
        }
    elif reference == "project":
        changes = {"project_id": project_seed(foreign), "project_snapshot": {"name": "Fictional"}}
    else:
        changes = {"document_id": document(foreign)}
    values = line(s, parent, **changes)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        save(s, values)
    assert failure.value.orig.sqlstate == "23503"
    assert snapshot(s) == before


@pytest.mark.parametrize("cause", ["asset", "direction", "too_many", "overflow", "blank"])
def test_document_shape_is_validated_at_commit_including_limits(ledger_setup, cause):
    s = ledger_setup
    parent = document(s)
    rows = [line(s, parent)]
    if cause == "asset":
        rows = [line(s, parent, asset="EUR")]
    elif cause == "direction":
        rows[0].update(category_id=s["expenses"][0].id, category_kind="expense")
    elif cause == "too_many":
        rows = [line(s, parent, line_no=i + 1) for i in range(201)]
    elif cause == "overflow":
        rows = [
            line(s, parent, PriceInput("1", "50000000000000000000"), line_no=i + 1)
            for i in range(2)
        ]
    else:
        rows = [dict(id=uuid4(), document_id=parent, ledger_id=s["ledger"])]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        save(s, *rows)
    assert failure.value.orig.diag.constraint_name == "ck_business_document_lines_shape"
    assert snapshot(s) == before


def test_header_edits_cannot_invalidate_existing_active_lines(ledger_setup):
    s = ledger_setup
    parent = document(s)
    save(s, line(s, parent))
    before = snapshot(s)
    for change in ({"asset_id": "EUR"}, {"document_kind": "bill"}):
        with pytest.raises(IntegrityError) as failure:
            with s["engine"].begin() as c:
                c.execute(
                    update(BusinessDocument)
                    .where(BusinessDocument.id == parent)
                    .values(version=2, **change)
                )
        assert failure.value.orig.diag.constraint_name == "ck_business_document_lines_shape"
        assert snapshot(s) == before


def test_project_snapshot_survives_rename_and_draft_repricing_is_versioned(ledger_setup):
    s = ledger_setup
    parent, project = document(s), project_seed(s)
    values = line(s, parent, project_id=project, project_snapshot={"name": "Fictional 项目"})
    save(s, values)
    value = PriceInput("4.00", "19.99", "9.97", "8.25")
    price = price_line(value, get_asset("USD"))
    with s["engine"].begin() as c:
        c.execute(
            update(BusinessProject)
            .where(BusinessProject.id == project)
            .values(version=2, name="Fictional renamed project")
        )
        c.execute(update(BusinessDocument).where(BusinessDocument.id == parent).values(version=2))
        c.execute(
            update(BusinessDocumentLine)
            .where(BusinessDocumentLine.id == values["id"])
            .values(
                version=2,
                **asdict(value),
                net_amount=price.net.to_decimal(),
                tax_amount=price.tax.to_decimal(),
                total_amount=price.total.to_decimal(),
            )
        )
    with s["engine"].connect() as c:
        row = c.execute(select(BusinessDocumentLine.__table__)).mappings().one()
        assert row["project_snapshot"] == values["project_snapshot"]
        assert row["quantity"] == "4.00" and row["version"] == 2
        assert row["total_amount"] == Decimal("75.76")


def test_stable_document_positions_are_unique(ledger_setup):
    s = ledger_setup
    values = line(s, document(s))
    save(s, values)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        save(s, values | {"id": uuid4()})
    assert failure.value.orig.diag.constraint_name == "uq_business_document_lines_position"
    assert snapshot(s) == before


@pytest.mark.parametrize("mutation", ["delete", "position", "version", "demote", "parent"])
def test_line_identity_and_original_inputs_cannot_be_erased(ledger_setup, mutation):
    s = ledger_setup
    values = line(s, document(s))
    save(s, values)
    changes = {"version": 2}
    if mutation == "position":
        changes["line_no"] = 2
    elif mutation == "version":
        changes["version"] = 3
    elif mutation == "parent":
        changes["document_id"] = uuid4()
    elif mutation == "demote":
        changes.update({key: None for key, _ in draft_line_migration()["_FIELDS"]})
    statement = (
        delete(BusinessDocumentLine)
        if mutation == "delete"
        else update(BusinessDocumentLine).values(**changes)
    ).where(BusinessDocumentLine.id == values["id"])
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            c.execute(statement)
    assert failure.value.orig.diag.constraint_name == "ck_business_reference_identity"
    assert snapshot(s) == before


def test_legacy_round_trip_and_archived_line_downgrade_guard(ledger_setup):
    s = ledger_setup
    _, old_doc, old_line = legacy_seed(s)
    before = snapshot(s)
    with s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            draft_line_migration()["downgrade"]()
            draft_line_migration()["upgrade"]()
    assert snapshot(s) == before
    initial(s, {"document_id": old_doc, "document_line_id": old_line})
    values = line(s, document(s))
    save(s, values)
    with s["engine"].begin() as c:
        c.execute(
            update(BusinessDocumentLine)
            .where(BusinessDocumentLine.id == values["id"])
            .values(version=2, archived=True)
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                draft_line_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_draft_line_downgrade"
    assert snapshot(s) == before
