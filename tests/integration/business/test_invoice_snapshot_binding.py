"""Real company facts bind a closed reservation without allocating a committed number."""

import hashlib
import json
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_current import BusinessDocumentCurrentReads
from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.drafts import DraftService
from coinpup_api.business.invoice_issuance_hash import invoice_issuance_hash_v2
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_profiles import InvoiceProfileService
from coinpup_api.business.invoice_render_snapshot import freeze_invoice_render_input
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.files.models import StoredFile
from coinpup_api.files.service import DocumentService
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.schemas import CategoryCreate
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_invoice_profile_storage import add, file
from tests.integration.business.test_invoice_profile_storage import setup as setup
from tests.integration.business.test_refund_revision_headers import schema_catalog
from tests.integration.conftest import invoice_snapshot_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@pytest.fixture
def company(setup):
    s = setup | {"ledger": setup["profile_ledger"]}
    s["income"] = s["structure"].create_category(
        s["owner"], s["ledger"], CategoryCreate(name="Fictional income", kind="income")
    )
    party = BusinessService(s["engine"]).create_party(
        s["owner"], s["ledger"], PartyCreate(id=uuid4(), name="Fictional 客户", role="customer")
    )

    def payload(**changes):
        return BusinessDraftCreate(
            **(
                dict(
                    id=uuid4(),
                    document_kind="invoice",
                    party_id=party.id,
                    asset_id="USD",
                    issue_date="2026-11-13",
                    lines=[
                        dict(
                            id=uuid4(),
                            description="Fictional 公司服务",
                            quantity="1",
                            unit_price="0.01",
                            discount_amount="0",
                            tax_rate_percent="0",
                            category_id=s["income"].id,
                            recognition_date="2026-11-13",
                        )
                    ],
                )
                | changes
            )
        )

    return s | dict(payload=payload, service=DraftService(s["engine"]))


def reviewed(s, count=1, *, logo=False, uppercase=False):
    identifier = file(s) if logo else None
    add(s, logo_file_id=identifier, logo_file_version=1 if logo else None)
    document, _ = confirmed(s, "invoice", count)
    if count == 200:
        send(
            s,
            document,
            correction(document).model_copy(
                update={
                    "replacement": replacement(
                        document, {document.lines[-1].id: {"unit_price": "0"}}
                    )
                }
            ),
            "fictional-snapshot-zero-tail",
        )
    current = BusinessDocumentCurrentReads(s["engine"]).current(
        s["owner"], s["ledger"], document.id
    )
    profile = InvoiceProfileService(s["engine"]).get(s["owner"], s["ledger"], s["profile"]["id"])
    command = InvoiceIssuanceCreate(
        id=uuid4(),
        document_id=document.id,
        expected_document_version=current.version,
        configuration_id=profile.id,
        expected_configuration_version=profile.version,
        previous_issuance_id=None,
    )
    original = command.model_dump(mode="json")
    if uppercase:
        original["id"] = original["id"].upper()
    raw = canonical(original)
    content = freeze_invoice_render_input(
        current,
        ASSETS["USD"],
        profile,
        ledger_id=s["ledger"],
        command=command,
        sequence="1",
        logo=DocumentService(s["engine"]).get_file(s["owner"], s["ledger"], identifier)
        if logo
        else None,
    )
    return dict(
        id=command.id,
        ledger_id=s["ledger"],
        document_id=document.id,
        document_version=current.version,
        configuration_id=profile.id,
        configuration_version=profile.version,
        sequence=1,
        number="虚构-000001",
        template_version="commercial-invoice-v1",
        snapshot_json=content.content.decode(),
        snapshot_sha256=content.sha256,
        request_json=raw,
        request_hash=invoice_issuance_hash_v2(s["ledger"], command, raw_body=raw.encode()),
        hash_version=2,
        idempotency_key="fictional-reviewed-reservation",
        created_by=s["owner"],
        logo_file_id=identifier,
    )


def reject(s, values, constraint):
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**values))
    assert caught.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize("count,logo,uppercase", [(1, False, False), (200, True, True)])
def test_complete_real_company_input_passes_binding_but_still_cannot_commit(
    company, count, logo, uppercase
):
    s = company
    values = reviewed(s, count, logo=logo, uppercase=uppercase)
    facts = json.loads(values["snapshot_json"])
    assert len(facts["document"]["current"]["document"]["lines"]) == count
    if count == 200:
        assert facts["document"]["current"]["document"]["lines"][-1]["total_amount"] == "0.00"
        assert len(facts["document"]["current"]["postings"]) == 199
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(InvoiceReservation).values(**values))
        assert c.scalar(select(InvoiceReservation.snapshot_json)) == values["snapshot_json"]
    assert caught.value.orig.diag.constraint_name == "ck_invoice_reservation_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_json", "{}"),
        ("snapshot_json", "[]"),
        ("snapshot_json", "{"),
        ("snapshot_json", '{"duplicate":1,"duplicate":2}'),
        ("snapshot_sha256", "0" * 64),
        ("request_hash", "0" * 64),
        ("request_json", "{}"),
        ("request_json", "null"),
    ],
)
def test_input_cannot_be_missing_malformed_or_bound_to_another_hash(company, field, value):
    s = company
    values = reviewed(s) | {field: value}
    reject(
        s,
        values,
        "ck_invoice_snapshot_intent" if field == "request_hash" else "ck_invoice_snapshot_binding",
    )


@pytest.mark.parametrize(
    "path,value,constraint",
    [
        (("intent", "id"), "00000000-0000-4000-8000-000000000001", "intent"),
        (("intent", "expected_document_version"), 3, "intent"),
        (("number", "sequence"), 1, "intent"),
        (("number", "number"), "Fictional-forged", "intent"),
        (("document", "current", "ledger_id"), "00000000-0000-4000-8000-000000000001", "intent"),
        (("document", "current", "document_id"), "00000000-0000-4000-8000-000000000001", "intent"),
        (("document", "current", "status"), "cancelled", "intent"),
        (("profile", "footer"), "Fictional forged footer", "profile"),
        (("profile", "created_at"), "2026-01-01T00:00:00Z", "profile"),
        (("profile", "extra"), "Fictional", "profile"),
        (("logo",), {"file_id": "00000000-0000-4000-8000-000000000001"}, "logo"),
        (("schema_version",), 2, "binding"),
        (("template_version",), "unreviewed-v2", "binding"),
        (("extra",), True, "binding"),
    ],
)
def test_rehashing_forged_current_input_does_not_bypass_owned_fact_binding(
    company, path, value, constraint
):
    s = company
    values = reviewed(s)
    data = json.loads(values["snapshot_json"])
    target = data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    values["snapshot_json"] = canonical(data)
    values["snapshot_sha256"] = hashlib.sha256(values["snapshot_json"].encode()).hexdigest()
    reject(s, values, "ck_invoice_snapshot_" + constraint)


@pytest.mark.parametrize("fault", ["hash", "size", "review", "header", "archive", "title"])
def test_reviewed_logo_binds_immutable_bytes_but_not_later_editable_title(company, fault):
    s = company
    values = reviewed(s, logo=True)
    data = json.loads(values["snapshot_json"])
    if fault in {"archive", "title"}:
        with s["engine"].begin() as c:
            c.execute(
                update(StoredFile)
                .where(StoredFile.id == values["logo_file_id"])
                .values(
                    version=2,
                    **({"archived": True} if fault == "archive" else {"title": "Later title"}),
                )
            )
    elif fault == "header":
        values["logo_file_id"] = None
    else:
        data["logo"][
            {"hash": "sha256", "size": "byte_size", "review": "reviewed_version"}[fault]
        ] = "b" * 64 if fault == "hash" else 2
        values["snapshot_json"] = canonical(data)
        values["snapshot_sha256"] = hashlib.sha256(values["snapshot_json"].encode()).hexdigest()
    reject(
        s,
        values,
        "ck_invoice_reservation_complete"
        if fault == "title"
        else "ck_invoice_snapshot_profile"
        if fault == "header"
        else "ck_invoice_snapshot_logo",
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        True,
        -9223372036854775808,
        9223372036854775807,
        {"虚构": ['é𐀀中\n\t\b\f\r"\\/', 9007199254740993], "a": {"b": False}},
    ],
)
def test_postgres_exact_canonical_bytes_and_hash_match_python(company, value):
    body = canonical(value)
    with company["engine"].connect() as c:
        result = c.execute(
            text(
                "SELECT coinpup_invoice_canonical_json(CAST(:body AS jsonb)) value, "
                "encode(sha256(convert_to(coinpup_invoice_canonical_json(CAST(:body AS jsonb)),"
                "'UTF8')),'hex') digest"
            ),
            {"body": body},
        ).one()
    assert result.value == body
    assert result.digest == hashlib.sha256(body.encode()).hexdigest()


def test_empty_binding_roundtrip_preserves_whole_catalog_and_rows(company):
    before = snapshot(company)
    with company["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = schema_catalog(c)
        migration = invoice_snapshot_migration()
        migration["downgrade"]()
        assert (
            c.scalar(text("SELECT to_regprocedure('coinpup_invoice_canonical_json(jsonb)')"))
            is None
        )
        migration["upgrade"]()
        assert schema_catalog(c) == catalog
    assert snapshot(company) == before


@pytest.mark.parametrize("raw", ['{"n":1.5}', '{"n":1.0}', '{"n":1e-2}'])
def test_sql_canonicalization_refuses_non_integer_json_numbers(company, raw):
    with pytest.raises(IntegrityError) as caught, company["engine"].begin() as c:
        c.execute(text("SELECT coinpup_invoice_canonical_json(CAST(:raw AS jsonb))"), {"raw": raw})
    assert caught.value.orig.diag.constraint_name == "ck_invoice_snapshot_canonical"


@pytest.mark.parametrize("body", ["whitespace", "duplicate", "other_intent", "unknown"])
def test_original_request_cannot_be_rewritten_after_its_hash_was_chosen(company, body):
    s = company
    values = reviewed(s)
    raw = values["request_json"]
    if body == "whitespace":
        values["request_json"] = " " + raw
    elif body == "duplicate":
        values["request_json"] = raw[:-1] + ',"id":' + json.dumps(str(values["id"])) + "}"
    else:
        parsed = json.loads(raw)
        parsed["id" if body == "other_intent" else "unknown"] = str(uuid4())
        values["request_json"] = canonical(parsed)
    reject(
        s,
        values,
        "ck_invoice_snapshot_intent" if body == "other_intent" else "ck_invoice_snapshot_binding",
    )


def test_even_notification_only_history_prevents_binding_downgrade(company):
    s = company
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_invoice_reservations "
            "(id uuid,ledger_id uuid,version int) ON COMMIT DROP"
        )
        c.exec_driver_sql(
            "CREATE TRIGGER fictional_invoice_history AFTER INSERT "
            "ON pg_temp.business_invoice_reservations FOR EACH ROW "
            "EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_invoice_reservations VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        invoice_snapshot_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_snapshot_downgrade"
    assert snapshot(s) == before


def test_protective_downgrade_cannot_discard_uncommitted_valid_input(company):
    s = company
    values = reviewed(s)
    before = snapshot(s)
    with (
        pytest.raises(IntegrityError) as caught,
        s["engine"].begin() as c,
        Operations.context(MigrationContext.configure(c)),
    ):
        c.execute(insert(InvoiceReservation).values(**values))
        invoice_snapshot_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_invoice_snapshot_downgrade"
    assert snapshot(s) == before
