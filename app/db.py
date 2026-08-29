"""SQLAlchemy engine and session initialization."""

from collections.abc import Iterator
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for application persistence models."""


def _configure_sqlite_connection(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    """Apply required SQLite settings to every newly opened connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def build_engine(
    settings: Settings | None = None,
    **engine_options: Any,
) -> Engine:
    """Build an engine without opening a database connection eagerly."""
    active_settings = settings or get_settings()
    url = make_url(active_settings.database_url)
    connect_args = (
        {"check_same_thread": False} if url.drivername.startswith("sqlite") else {}
    )
    engine = create_engine(
        active_settings.database_url,
        connect_args=connect_args,
        **engine_options,
    )
    if url.drivername.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to the supplied engine."""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def get_db_session(request: Request) -> Iterator[Session]:
    """Yield one request-scoped database session from the active application."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


engine = build_engine()
SessionLocal = build_session_factory(engine)
