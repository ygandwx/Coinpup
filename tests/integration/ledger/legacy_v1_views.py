"""Only the real 0008 fixture uses its reflected pre-dimension journal shape."""

from contextlib import contextmanager

from coinpup_api.ledger.models import JournalLine
from sqlalchemy import MetaData, Table, event
from sqlalchemy.orm import Session, load_only, registry

# Frozen names, deliberately independent of the current storage dimension constants.
DIMENSIONS = (
    "project_id",
    "party_id",
    "counterparty_entity_id",
    "document_id",
    "document_line_id",
    "dimension_owner_id",
)
OLD_LINE_COLUMNS = (
    "id",
    "journal_id",
    "ledger_id",
    "line_no",
    "component_no",
    "role",
    "asset_id",
    "amount",
    "account_id",
    "category_id",
)


def historical_line_model(engine):
    table = Table("journal_lines", MetaData(), autoload_with=engine)
    assert set(table.c.keys()) == set(OLD_LINE_COLUMNS)

    class HistoricalLine:
        pass

    registry().map_imperatively(HistoricalLine, table)
    return HistoricalLine


@contextmanager
def historical_reader_session(engine):
    with Session(engine) as session:

        @event.listens_for(session, "do_orm_execute")
        def frozen_line_projection(state):
            if state.is_select and state.bind_mapper is not None:
                if state.bind_mapper.class_ is JournalLine:
                    state.statement = state.statement.options(
                        load_only(*(getattr(JournalLine, name) for name in OLD_LINE_COLUMNS))
                    )

        yield session
