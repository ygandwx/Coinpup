"""Prepared source statements recompute the same immutable facts after each transaction change."""

import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_fact_validation import pending_fact, pending_origin
from tests.integration.business.test_credit_pinned_validation import terminal
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_source_plan_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def compare_query(c, query, function, journal_column, ledger, credits, journals):
    sql = query.split("AS $function$", 1)[1].rsplit("$function$", 1)[0].strip().removesuffix(";")
    for name, parameter in (
        ("ledger_identifier", "CAST(:ledger AS uuid)"),
        ("credit_identifiers", "CAST(:credits AS uuid[])"),
        ("journal_identifiers", "CAST(:journals AS uuid[])"),
    ):
        sql = re.sub(r"\b" + name + r"\b", parameter, sql)
    params = dict(ledger=ledger, credits=credits, journals=journals)
    expected = c.execute(
        text(f"SELECT * FROM ({sql}) exact_source ORDER BY {journal_column}"), params
    ).all()
    result = c.execute(
        text(
            f"SELECT * FROM {function}(CAST(:ledger AS uuid),"
            "CAST(:credits AS uuid[]),CAST(:journals AS uuid[])) ORDER BY journal_id"
        ),
        params,
    ).all()
    assert result == expected
    return result


def compare(c, ledger, credits, journals):
    migration = credit_source_plan_migration()
    compare_query(
        c, migration["_OLD_POSTINGS"], "coinpup_credit_posting_facts", 11, ledger, credits, journals
    )
    return compare_query(
        c, migration["_OLD"], "coinpup_credit_document_sources", 3, ledger, credits, journals
    )


@pytest.mark.parametrize("planning", ["force_generic_plan", "force_custom_plan"])
def test_reused_plan_returns_new_sources_without_caching_old_rows_or_leaking_scope(
    sources, planning
):
    s, h, doc, confirmed, original, ch, payload = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as gated, s["engine"].begin() as c:
        c.execute(text("SELECT set_config('plan_cache_mode',:mode,true)"), dict(mode=planning))
        baseline = compare(c, s["ledger"], [original.id], None)
        assert len(baseline) == 1
        # Exercise reuse before adding new versioned source facts in the same transaction.
        for _ in range(6):
            assert compare(c, s["ledger"], None, [original.postings[0].journal_id]) == baseline
        first = append_accrual(c, h, doc, confirmed.postings[0], "correct", 2)
        new_id = pending_origin(
            c, original, header_changes=dict(source_version=h["document_version"])
        )
        member = pending_fact(c, ch, original)
        complete = compare(c, s["ledger"], [original.id, new_id], None)
        assert len(complete) == 3 and all(row[3] == s["ledger"] for row in complete)
        assert len(compare(c, s["ledger"], None, [member["journal_id"]])) == 1
        for ledger, ids, journals in (
            (uuid4(), [original.id, new_id], None),
            (s["ledger"], [], None),
            (s["ledger"], None, []),
            (s["ledger"], None, None),
            (s["ledger"], [original.id], [uuid4()]),
        ):
            assert compare(c, ledger, ids, journals) == []
        append_accrual(c, terminal(h), doc, SimpleNamespace(**first), "cancel", 3)
        assert compare(c, s["ledger"], [original.id, new_id], None) == complete
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert gated.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert credit(s, payload) == original and snapshot(s) == before


def test_empty_source_plan_migration_restores_every_function_and_keeps_invoker_scope(sources):
    s, _, _, _, original, _, payload = sources
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        functions = ("coinpup_credit_posting_facts", "coinpup_credit_document_sources")

        def metadata():
            return c.execute(
                text(
                    "SELECT p.proname,l.lanname,p.provolatile,p.prosecdef,p.proconfig "
                    "FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang "
                    "WHERE p.pronamespace='public'::regnamespace AND p.proname IN (:first,:second) "
                    "ORDER BY p.proname"
                ),
                dict(first=functions[0], second=functions[1]),
            ).all()

        assert metadata() == [
            (name, "plpgsql", "s", False, ["search_path=pg_catalog"]) for name in sorted(functions)
        ]
        old = compare(c, s["ledger"], [original.id], None)
        migration = credit_source_plan_migration()
        migration["downgrade"]()
        assert metadata() == [
            (name, "sql", "s", False, ["search_path=pg_catalog"]) for name in sorted(functions)
        ]
        assert compare(c, s["ledger"], [original.id], None) == old
        migration["upgrade"]()
        assert definitions(c) == baseline
    assert credit(s, payload) == original and snapshot(s) == before


def test_source_plan_downgrade_protects_versioned_history(sources):
    s, h, doc, confirmed, _, _, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        append_accrual(c, h, doc, confirmed.postings[0], "correct", 2)
        with Operations.context(MigrationContext.configure(c)):
            credit_source_plan_migration()["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_credit_source_plan_downgrade"
    assert snapshot(s) == before
