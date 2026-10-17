import os
from logging.config import fileConfig

from sqlalchemy import create_engine

from alembic import context
from parent.models import Base

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

with create_engine(os.environ["DATABASE_URL"]).connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
