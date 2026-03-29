from __future__ import annotations

import os
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Optional[Engine] = None


def get_engine() -> Engine:
    """Create or return the SQLAlchemy engine from DATABASE_URL env var."""
    global _engine
    if _engine is None:
        database_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/legaldocs",
        )
        _engine = create_engine(database_url, pool_pre_ping=True)
    return _engine


def get_session() -> Generator[Session, None, None]:
    """Yield a database session."""
    engine = get_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
