"""Migration URL comes from the same configuration as the application."""

from alembic import context
from coinpup_api.config import Settings
from coinpup_api.files import models as document_models  # noqa: F401
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.models import Base
from sqlalchemy import create_engine, pool

target_metadata = Base.metadata
database_url = Settings().database_url.get_secret_value()

if context.is_offline_mode():
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
