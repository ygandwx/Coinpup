"""Master data ownership, optimistic concurrency and unknown-create reconciliation."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.admin import create_admin
from coinpup_api.business.models import BusinessParty
from coinpup_api.business.schemas import (
    PartyCreate,
    PartyResponse,
    PartyUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
)
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.schemas import EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import insert

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture(params=["party", "project"])
def catalog(ledger_setup, request):
    s = ledger_setup
    service = BusinessService(s["engine"])
    kind = request.param
    create_schema, update_schema, response_schema = (
        (PartyCreate, PartyUpdate, PartyResponse)
        if kind == "party"
        else (ProjectCreate, ProjectUpdate, ProjectResponse)
    )

    def payload(**values):
        body = dict(id=uuid4(), name="Fictional 测试资料", notes="Fictional notes")
        if kind == "party":
            body["role"] = "both"
        return create_schema(**(body | values))

    return s | dict(
        service=service,
        kind=kind,
        payload=payload,
        update_schema=update_schema,
        response_schema=response_schema,
        create=getattr(service, "create_" + kind),
        get=getattr(service, "get_" + kind),
        update=getattr(service, "update_" + kind),
        list=getattr(service, "list_parties" if kind == "party" else "list_projects"),
    )


def test_unknown_create_reuses_id_without_overwriting(catalog):
    c = catalog
    payload = c["payload"]()
    original = c["create"](c["owner"], c["ledger"], payload)
    before = snapshot(c)
    for retry in (payload, c["payload"](id=payload.id, name="Fictional conflicting intent")):
        with pytest.raises(LedgerError) as error:
            c["create"](c["owner"], c["ledger"], retry)
        assert error.value.code == "duplicate_record"
        assert c["get"](c["owner"], c["ledger"], payload.id) == original
        assert snapshot(c) == before


def test_stale_and_concurrent_edits_preserve_one_winner(catalog):
    c = catalog
    row = c["create"](c["owner"], c["ledger"], c["payload"]())

    def change(name):
        try:
            return c["update"](
                c["owner"], c["ledger"], row.id, c["update_schema"](expected_version=1, name=name)
            ).name
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ["Fictional first", "Fictional second"]))
    assert results.count("version_conflict") == 1
    current = c["get"](c["owner"], c["ledger"], row.id)
    assert current.version == 2 and current.name in results
    before = snapshot(c)
    assert change("Fictional stale") == "version_conflict"
    assert snapshot(c) == before


def test_scope_and_archived_entity_protect_writes_without_hiding_reads(catalog):
    c = catalog
    row = c["create"](c["owner"], c["ledger"], c["payload"]())
    other_owner = create_admin(
        c["engine"], "fictional-other-owner", "fictional-owner-password-2026"
    )
    other_ledger = (
        c["structure"]
        .create_entity(
            c["owner"],
            EntityCreate(kind="personal", name="Fictional other ledger", base_asset_id="USD"),
        )
        .ledger.id
    )
    before = snapshot(c)
    for owner, ledger in ((other_owner, c["ledger"]), (c["owner"], other_ledger)):
        for action in (
            lambda owner=owner, ledger=ledger: c["get"](owner, ledger, row.id),
            lambda owner=owner, ledger=ledger: c["update"](
                owner, ledger, row.id, c["update_schema"](expected_version=1, archived=True)
            ),
        ):
            with pytest.raises(LedgerError) as error:
                action()
            assert error.value.code == "not_found"
    assert snapshot(c) == before
    c["structure"].update_entity(
        c["owner"], c["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(c)
    for action in (
        lambda: c["create"](c["owner"], c["ledger"], c["payload"]()),
        lambda: c["update"](
            c["owner"], c["ledger"], row.id, c["update_schema"](expected_version=1, notes=None)
        ),
    ):
        with pytest.raises(LedgerError) as error:
            action()
        assert error.value.code == "entity_archived"
    assert c["get"](c["owner"], c["ledger"], row.id) == row
    assert c["list"](c["owner"], c["ledger"]) == [row]
    assert snapshot(c) == before


def test_archive_restore_optional_clear_and_bounded_pagination(catalog):
    c = catalog
    first, second = [c["create"](c["owner"], c["ledger"], c["payload"]()) for _ in range(2)]
    archived = c["update"](
        c["owner"],
        c["ledger"],
        first.id,
        c["update_schema"](expected_version=1, archived=True, notes=None),
    )
    assert archived.archived and archived.notes is None and archived.version == 2
    assert c["list"](c["owner"], c["ledger"], include_archived=False) == [second]
    assert c["list"](c["owner"], c["ledger"], limit=1, offset=1) == [second]
    restored = c["update"](
        c["owner"], c["ledger"], first.id, c["update_schema"](expected_version=2, archived=False)
    )
    assert c["list"](c["owner"], c["ledger"], include_archived=False) == [restored, second]
    for page in (dict(limit=0), dict(limit=201), dict(offset=-1), dict(offset=100001)):
        with pytest.raises(LedgerError) as error:
            c["list"](c["owner"], c["ledger"], **page)
        assert error.value.code == "invalid_pagination"


def test_failure_after_flush_rolls_back_record_and_notification(catalog, monkeypatch):
    c = catalog
    before = snapshot(c)

    def fail(cls, value):
        raise RuntimeError("Fictional response failure")

    monkeypatch.setattr(c["response_schema"], "model_validate", classmethod(fail))
    with pytest.raises(RuntimeError, match="Fictional response failure"):
        c["create"](c["owner"], c["ledger"], c["payload"]())
    assert snapshot(c) == before


def test_legacy_party_requires_explicit_complete_profile(ledger_setup):
    s = ledger_setup
    service, identifier = BusinessService(s["engine"]), uuid4()
    with s["engine"].begin() as connection:
        connection.execute(insert(BusinessParty).values(id=identifier, ledger_id=s["ledger"]))
    legacy = service.get_party(s["owner"], s["ledger"], identifier)
    assert legacy.name is None and legacy.role is None
    before = snapshot(s)
    for changes in (
        dict(name="Fictional incomplete"),
        dict(role="customer"),
        dict(notes="Fictional incomplete"),
    ):
        with pytest.raises(LedgerError) as error:
            service.update_party(
                s["owner"], s["ledger"], identifier, PartyUpdate(expected_version=1, **changes)
            )
        assert error.value.code == "party_profile_incomplete"
        assert snapshot(s) == before
    completed = service.update_party(
        s["owner"],
        s["ledger"],
        identifier,
        PartyUpdate(expected_version=1, name="Fictional 补全", role="customer"),
    )
    assert completed.version == 2 and completed.id == legacy.id
    assert completed.created_at == legacy.created_at
