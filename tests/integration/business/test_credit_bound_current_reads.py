"""A group's current metadata uses its own bound source, even after later document changes."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.credit_receipts import response_from_rows
from coinpup_api.business.credit_revision_reads import read_current_credit_group
from coinpup_api.business.drafts import PARTY_SNAPSHOT_FIELDS, _snapshot
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_origin import all_rows
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_fact_validation import pending_fact, pending_origin
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("asset,unit", [("BTC", "0.00000001"), ("ETH", "0.000000000000000001")])
def test_original_and_revision_metadata_read_their_own_source_in_two_owned_selects(
    sources, asset, unit
):
    s, h, doc, confirmed, original, ch, payload = sources
    party = s["master"].create_party(
        s["owner"],
        s["ledger"],
        PartyCreate(id=uuid4(), name="Fictional bound source party", role="customer"),
    )
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {asset}}
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as gated, s["engine"].begin() as c, Session(c) as db:
        h = h | dict(
            asset_id=asset,
            party_id=party.id,
            party_snapshot=_snapshot(party, PARTY_SNAPSHOT_FIELDS),
        )
        fact = dict(
            quantity="1",
            unit_price=unit,
            discount_amount="0",
            tax_rate_percent="0",
            net_amount=unit,
            tax_amount="0",
            total_amount=unit,
        )
        first = append_accrual(c, h, doc, confirmed.postings[0], "correct", 2, fact_changes=fact)
        identifier = pending_origin(
            c,
            original,
            header_changes={"source_version": h["document_version"]},
            line_changes=dict(net_amount=unit, tax_amount="0", total_amount=unit),
            journal_line_changes=lambda row: dict(
                asset_id=asset, party_id=party.id, amount=Decimal(unit).copy_sign(row["amount"])
            ),
        )
        bound = response_from_rows(db, identifier)

        def read(target):
            seen = []

            def capture(_c, _cursor, statement, _parameters, _context, _many):
                seen.append(statement.upper().lstrip())

            previous = all_rows(c)
            event.listen(c, "before_cursor_execute", capture)
            try:
                result = read_current_credit_group(db, s["ledger"], target)
            finally:
                event.remove(c, "before_cursor_execute", capture)
            assert len(seen) == 2 and all(q.startswith("SELECT") for q in seen)
            assert all("FOR UPDATE" not in q and "PG_ADVISORY" not in q for q in seen)
            assert all_rows(c) == previous
            return result

        old = read(original.id)
        assert (old.asset_id, old.party_id, old.version) == ("USD", doc.party_id, 1)
        latest = read(identifier)
        assert (latest.asset_id, latest.party_id, latest.version) == (asset, party.id, 1)
        assert latest.members[0].total_amount == unit
        pending_fact(c, ch | dict(credit_id=identifier), bound)
        corrected = read(identifier)
        assert (corrected.asset_id, corrected.party_id, corrected.version) == (asset, party.id, 2)
        later = h | dict(
            id=uuid4(),
            previous_revision_id=h["id"],
            previous_version=h["document_version"],
            document_version=h["document_version"] + 1,
            idempotency_key="fictional-later-source",
            asset_id="USD",
            party_id=doc.party_id,
            party_snapshot=doc.party_snapshot,
        )
        append_accrual(c, later, doc, SimpleNamespace(**first), "correct", 3)
        assert read(original.id) == old and read(identifier) == corrected
        with pytest.raises(LedgerError) as denied:
            read_current_credit_group(db, uuid4(), identifier)
        assert (denied.value.code, denied.value.status) == ("not_found", 404)
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert gated.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert credit(s, payload) == original and snapshot(s) == before
