"""Binding cannot outlive or escape the explicitly owned transaction."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.transactions import BoundCommands, _Binding, bind_commands
from sqlalchemy.orm import Session


def test_binding_requires_existing_transaction_before_any_database_access():
    with Session() as session, pytest.raises(LedgerError) as error:
        bind_commands(session, uuid4(), [uuid4()], ["USD"])
    assert error.value.code == "ocr_transaction_invalid"


@pytest.mark.parametrize("case", ["owner", "ended", "nested", "ledger", "session"])
def test_binding_rejects_scope_or_transaction_changes(case):
    owner, ledger = uuid4(), uuid4()
    with Session() as session, session.begin():
        binding = _Binding(
            session, session.get_transaction(), owner, frozenset({ledger}), frozenset()
        )
        if case == "ended":
            session.rollback()
        nested = session.begin_nested() if case == "nested" else None
        try:
            with pytest.raises(LedgerError) as error:
                binding.ledger(
                    object() if case == "session" else session,
                    uuid4() if case == "owner" else owner,
                    uuid4() if case == "ledger" else ledger,
                )
            assert error.value.code == "ocr_transaction_invalid"
        finally:
            if nested is not None:
                nested.rollback()


@pytest.mark.parametrize("kind", ["correct", "cancel", "history", "invoice", "__dict__"])
def test_facade_never_dispatches_revision_or_new_business_commands(kind):
    # Invalid actions are rejected before requiring any service or session.
    commands = object.__new__(BoundCommands)
    with pytest.raises(LedgerError) as error:
        commands.post(kind, uuid4(), {}, "fictional")
    assert error.value.code == "ocr_transaction_invalid"
