"""Verify exact price inputs, membership, line history and the closed group gate on PostgreSQL."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmation_service import submit
from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_revision_line_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def line(header, doc):
    source = doc.lines[0]
    fields = set(Fact.__table__.columns.keys()).intersection(type(source).model_fields)
    return source.model_dump(include=fields - {"created_at", "version"}) | {
        "id": uuid4(),
        "source_line_id": source.id,
        "revision_id": header["id"],
        "confirmation_id": header["confirmation_id"],
        "document_version": header["document_version"],
        "line_no": 1,
    }


@pytest.mark.parametrize(
    "asset,unit,net,tax,total",
    [
        ("USD", "0.01", "0.02", "0.01", "0.03"),
        ("BTC", "0.00000001", "0.00000002", "0.00000001", "0.00000003"),
    ],
)
def test_two_hundred_repriced_ordered_lines_include_zero_and_cannot_escape_the_group(
    revision, asset, unit, net, tax, total
):
    s, header, doc, intent, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Header), header | {"asset_id": asset})
        first = line(header, doc) | dict(
            quantity="1.50",
            unit_price=unit,
            discount_amount="-0",
            tax_rate_percent="25.000",
            net_amount=net,
            tax_amount=tax,
            total_amount=total,
        )
        facts = [first]
        for pos in range(2, 201):
            facts.append(
                first
                | dict(
                    id=uuid4(),
                    source_line_id=uuid4(),
                    line_no=pos,
                    unit_price="-0",
                    net_amount="0",
                    tax_amount="0",
                    total_amount="0",
                )
            )
        c.execute(insert(Fact), facts)
        saved = c.execute(
            select(Fact.source_line_id, Fact.unit_price, Fact.tax_rate_percent).order_by(
                Fact.line_no
            )
        ).all()
        assert saved == [
            (r["source_line_id"], r["unit_price"], r["tax_rate_percent"]) for r in facts
        ]
        assert (
            c.scalar(
                select(func.count())
                .select_from(ChangeLog)
                .where(ChangeLog.entity_type == Fact.__tablename__)
            )
            == 200
        )
        c.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
    assert err.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert submit(s, doc, intent) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"document_id": uuid4()}, "fk_document_revision_line_header"),
        ({"confirmation_id": uuid4()}, "fk_document_revision_line_header"),
        ({"document_version": 20}, "fk_document_revision_line_header"),
        ({"line_no": 201}, "ck_document_revision_line_position"),
        ({"line_no": 0}, "ck_document_revision_line_position"),
        ({"description": " \n"}, "ck_document_revision_line_description_date"),
        ({"quantity": "0"}, "ck_document_revision_line_pricing"),
        ({"unit_price": "01"}, "ck_document_revision_line_pricing"),
        ({"unit_price": "1.230"}, "ck_document_revision_line_pricing"),
        ({"unit_price": "NaN"}, "ck_document_revision_line_pricing"),
        ({"quantity": "1e2"}, "ck_document_revision_line_pricing"),
        ({"unit_price": "-1"}, "ck_document_revision_line_pricing"),
        ({"discount_amount": "1000"}, "ck_document_revision_line_pricing"),
        ({"discount_amount": "-1"}, "ck_document_revision_line_pricing"),
        ({"tax_rate_percent": "-1"}, "ck_document_revision_line_pricing"),
        ({"tax_amount": "0.001"}, "ck_document_revision_line_pricing"),
        ({"net_amount": "NaN"}, "ck_document_revision_line_pricing"),
        ({"total_amount": "110.01"}, "ck_document_revision_line_pricing"),
        ({"category_kind": "expense"}, "ck_document_revision_line_replacement"),
        ({"category_snapshot": {}}, "ck_document_revision_line_snapshots"),
        ({"project_id": None}, "ck_document_revision_line_snapshots"),
    ],
)
def test_direct_invalid_line_facts_roll_back_header_and_feed(revision, changes, expected):
    s, header, doc, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Header), header)
        c.execute(insert(Fact), line(header, doc) | changes)
    assert err.value.orig.diag.constraint_name == expected and snapshot(s) == before


@pytest.mark.parametrize(
    "op,expected",
    [
        ("update", "ck_document_revision_line_immutable"),
        ("delete", "ck_document_revision_line_immutable"),
        ("source", "uq_document_revision_line_source"),
        ("position", "uq_document_revision_line_position"),
        ("cancelled", "ck_document_revision_line_replacement"),
        ("downgrade", "ck_document_revision_line_downgrade"),
    ],
)
def test_line_history_positions_and_cancel_shape_are_immutable(revision, op, expected):
    s, header, doc, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.execute(insert(Header), cancelled(header) if op == "cancelled" else header)
        member = line(header, doc)
        c.execute(insert(Fact), member)
        if op == "update":
            c.execute(update(Fact).values(description="Fictional changed"))
        elif op == "delete":
            c.execute(delete(Fact))
        elif op in ("source", "position"):
            diff = {"line_no": 2} if op == "source" else {"source_line_id": uuid4()}
            c.execute(insert(Fact), member | {"id": uuid4()} | diff)
        elif op == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                # The populated frozen 0076 refuses before modifying its table or later FKs.
                document_revision_line_migration(include_dependents=False)["downgrade"]()
    assert err.value.orig.diag.constraint_name == expected and snapshot(s) == before


def test_line_change_history_alone_prevents_downgrade(revision):
    s, header, *_ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_document_revision_lines"
            "(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
        )
        c.exec_driver_sql(
            "CREATE TRIGGER fictional_line AFTER INSERT ON "
            "pg_temp.business_document_revision_lines FOR EACH ROW "
            "EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_document_revision_lines VALUES (:id,:ledger,1)"),
            dict(id=uuid4(), ledger=header["ledger_id"]),
        )
        with Operations.context(MigrationContext.configure(c)):
            document_revision_line_migration()["downgrade"]()
    assert err.value.orig.diag.constraint_name == "ck_document_revision_line_downgrade"
    assert snapshot(s) == before
