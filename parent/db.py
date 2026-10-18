from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(create_engine(database_url, pool_pre_ping=True), expire_on_commit=False)
