from __future__ import annotations

import os
import re
from typing import Generator, Optional

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from parent.db import get_engine


_TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def get_tenant_id(x_tenant_id: Optional[str] = Header(None)) -> str:
    """Extract and validate X-Tenant-ID from request header.

    Returns the validated tenant_id string.
    Raises HTTPException(400) if missing, empty, too long, or invalid format.
    """
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")
    if len(x_tenant_id) > 64:
        raise HTTPException(
            status_code=400, detail="X-Tenant-ID must be at most 64 characters"
        )
    if not _TENANT_ID_PATTERN.match(x_tenant_id):
        raise HTTPException(
            status_code=400,
            detail="X-Tenant-ID must contain only alphanumeric characters, hyphens, and underscores",
        )
    return x_tenant_id


def get_db() -> Generator[Session, None, None]:
    """Yield a database session for FastAPI dependency injection."""
    from sqlalchemy.orm import sessionmaker

    engine = get_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_config() -> dict:
    """Load configuration from environment variables."""
    return {
        "database_url": os.environ.get(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/legaldocs"
        ),
        "redis_url": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "ca_cert_path": os.environ.get("CA_CERT_PATH", "certs/ca.crt"),
        "enclave_cert_path": os.environ.get("ENCLAVE_CERT_PATH", "certs/enclave.crt"),
        "enclave_key_path": os.environ.get("ENCLAVE_KEY_PATH", "certs/enclave.key"),
        "parent_cert_path": os.environ.get("PARENT_CERT_PATH", "certs/parent.crt"),
        "parent_key_path": os.environ.get("PARENT_KEY_PATH", "certs/parent.key"),
    }
