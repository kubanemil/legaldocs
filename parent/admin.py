"""Operator commands: create a tenant with an API key, or revoke a key."""

import argparse
import os
import re
from typing import Final

from sqlalchemy.orm import Session

from parent import auth
from parent.db import session_factory
from parent.models import ApiKey, Tenant

TENANT_ID: Final = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def create_tenant(session: Session, tenant_id: str) -> auth.IssuedKey:
    if not TENANT_ID.fullmatch(tenant_id):
        raise ValueError("tenant id must be 2-63 lowercase letters, digits or dashes")
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id))
        session.flush()
    issued = auth.issue()
    session.add(ApiKey(id=issued.key_id, tenant_id=tenant_id, secret_hash=issued.secret_hash))
    session.commit()
    return issued


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "create-tenant", help="create a tenant (if new) and issue an API key"
    ).add_argument("tenant_id")
    commands.add_parser("revoke-key", help="revoke an API key by its id").add_argument("key_id")
    args = parser.parse_args()

    with session_factory(os.environ["DATABASE_URL"])() as session:
        if args.command == "create-tenant":
            issued = create_tenant(session, args.tenant_id)
            print(issued.token)
        elif not auth.revoke(session, args.key_id):
            raise SystemExit(f"no active key {args.key_id}")


if __name__ == "__main__":
    main()
