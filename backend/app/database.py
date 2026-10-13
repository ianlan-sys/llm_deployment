from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def configure_database(database_url: str) -> Engine:
    """Configure the process-local SQLite session factory."""
    global _engine, _session_factory

    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, connect_args=connect_args)
    _session_factory = sessionmaker(bind=_engine)
    return _engine


def get_session() -> Iterator[Session]:
    if _session_factory is None:
        raise RuntimeError("Database has not been configured")
    with _session_factory() as session:
        yield session
