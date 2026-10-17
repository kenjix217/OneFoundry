"""Alembic online migrations against the configured local database."""

from alembic import context
from sqlalchemy import create_engine

from onefoundry.settings import Settings


def run_migrations_online() -> None:
    engine = create_engine(Settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
