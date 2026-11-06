"""Pending fixed-version replay reads only; the financial revision gate still rejects commit."""

from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.business.credit_revision_hash import credit_revision_hash_v2
from coinpup_api.business.credit_revision_receipts import replay_revision, response_from_rows
from coinpup_api.business.credit_revision_schemas import CreditRevisionResponse
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, Entity
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from tests.integration.business.credit_transition_fixture import group
from tests.integration.business.test_credit_projections import gated
from tests.integration.business.test_credit_revision_receipts import (
    candidate,
    content,
    save_receipt,
)
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_schema import write_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.unit.business.test_credit_revision_replay import intent

pytestmark = pytest.mark.integration


def command(connection, state, sources, **kwargs):
    header, _, expected = candidate(connection, state, sources, **kwargs)
    response = CreditRevisionResponse.model_validate(expected)
    payload = intent(response)
    raw = payload.model_dump_json().encode()
    digest = credit_revision_hash_v2(
        response.action, response.ledger_id, response.credit_id, payload, raw_body=raw
    )
    save_receipt(connection, header, expected, request_hash=digest)
    return response, payload, raw, digest, header["idempotency_key"]


def replay(session, saved, *, ledger_id=None, credit_id=None, digest=None):
    response, payload, _, expected_digest, key = saved
    receipt = session.get(CommandReceipt, (response.ledger_id, key))
    return replay_revision(
        session,
        receipt,
        ledger_id or response.ledger_id,
        credit_id or response.credit_id,
        response.action,
        payload,
        digest or expected_digest,
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("closed", [False, True])
def test_fixed_original_intents_remain_read_only_after_later_members_cancel_and_closure(
    drafts, kind, closed
):
    s = drafts
    state = group(s, kind=kind)
    a, b, c = state["source_ids"]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        commands = []
        for index, sources in enumerate(([a, c], [b, c], None)):
            commands.append(
                command(
                    connection,
                    state,
                    sources,
                    header_changes=(
                        dict(
                            credit_date=date(2026, 11, 4 + index),
                            credit_reason=f"Fictional revision fact {index} 中文",
                        )
                        if sources
                        else {}
                    ),
                )
            )
        if closed:
            write_period(connection, s, new=date(2026, 11, 6))
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_ledger_periods_history, tr_ledger_period_audits_history, "
                "tr_ledger_period_receipts_history IMMEDIATE"
            )
            connection.execute(
                update(AssetRecord)
                .where(AssetRecord.asset_id == "USD")
                .values(enabled=False, version=AssetRecord.version + 1)
            )
            connection.execute(
                update(Entity)
                .where(Entity.id == s["entity"].id)
                .values(archived=True, version=Entity.version + 1)
            )
        unchanged = content(connection)
        with Session(bind=connection) as session:
            for saved in commands:
                expected = saved[0]
                assert replay(session, saved).model_dump(mode="json") == expected.model_dump(
                    mode="json"
                )
                assert response_from_rows(session, expected.revision_id) == expected
                # A retry retains the raw spelling and fixed version even after cancellation.
                altered = saved[1].model_copy(update={"reason": "Fictional changed intention"})
                wrong_digest = credit_revision_hash_v2(
                    expected.action, expected.ledger_id, expected.credit_id, altered
                )
                with pytest.raises(LedgerError) as failure:
                    replay(session, saved, digest=wrong_digest)
                assert failure.value.code == "idempotency_conflict"
        assert content(connection) == unchanged
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "digest",
        "v1",
        "status",
        "anchor",
        "key",
        "credit",
        "ledger",
        "decimal_version",
        "old_total",
        "normalized_uuid",
    ],
)
def test_adapter_rejects_mismatched_receipt_or_scope_without_any_writes(drafts, fault):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        header, _, expected = candidate(connection, state, list(state["current"]))
        response = CreditRevisionResponse.model_validate(expected)
        payload = intent(response)
        digest = credit_revision_hash_v2(
            response.action, response.ledger_id, response.credit_id, payload
        )
        values = {"request_hash": digest}
        body = deepcopy(expected)
        if fault == "v1":
            values["hash_version"] = 1
        elif fault == "status":
            values["response_status"] = 201
        elif fault == "anchor":
            values["operation_id"] = response.members[1].operation_id
        elif fault == "key":
            values["key"] = str(uuid4())
        elif fault == "decimal_version":
            body["version"] = 2.0  # Version type corruption, never a money conversion.
        elif fault == "old_total":
            body["members"][0]["previous_total_amount"] = "21.000000000000000000"
        elif fault == "normalized_uuid":
            body["revision_id"] = response.revision_id.hex.upper()
        save_receipt(connection, header, body, **values)
        key = values.get("key", header["idempotency_key"])
        unchanged = content(connection)
        with Session(bind=connection) as session:
            receipt = session.get(CommandReceipt, (response.ledger_id, key))
            statements = []

            def record(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(connection, "before_cursor_execute", record)
            try:
                with pytest.raises(LedgerError) as failure:
                    replay_revision(
                        session,
                        receipt,
                        uuid4() if fault == "ledger" else response.ledger_id,
                        uuid4() if fault == "credit" else response.credit_id,
                        response.action,
                        payload,
                        "0" * 64 if fault == "digest" else digest,
                    )
            finally:
                event.remove(connection, "before_cursor_execute", record)
            conflict = fault in {"digest", "v1"}
            assert (failure.value.code, failure.value.status) == (
                ("idempotency_conflict", 409) if conflict else ("credit_revision_integrity", 503)
            )
            if conflict:
                assert statements == []
        assert content(connection) == unchanged
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_same_digest_with_different_validated_payload_cannot_claim_stored_amounts(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        saved = command(connection, state, list(state["current"]))
        response, payload, _, digest, key = saved
        bad = payload.model_copy(
            update={
                "replacement": payload.replacement.model_copy(
                    update={
                        "lines": [
                            payload.replacement.lines[0].model_copy(update={"net_amount": "1.00"}),
                            *payload.replacement.lines[1:],
                        ]
                    }
                )
            }
        )
        unchanged = content(connection)
        with Session(bind=connection) as session, pytest.raises(LedgerError) as failure:
            receipt = session.get(CommandReceipt, (response.ledger_id, key))
            replay_revision(
                session,
                receipt,
                response.ledger_id,
                response.credit_id,
                response.action,
                bad,
                digest,
            )
        assert failure.value.code == "credit_revision_integrity"
        assert content(connection) == unchanged
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_missing_fixed_sql_projection_maps_only_its_known_shape_failure(drafts):
    s = drafts
    state = group(s)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        saved = command(connection, state, list(state["current"]))
        unchanged = content(connection)
        with Session(bind=connection) as session:
            with pytest.raises(LedgerError) as failure, session.begin_nested():
                response_from_rows(session, uuid4())
            assert (failure.value.code, failure.value.status) == ("credit_revision_integrity", 503)
            # The SQL error rolls back its savepoint, not the caller's pending facts.
            assert replay(session, saved) == saved[0]
        assert content(connection) == unchanged
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before
