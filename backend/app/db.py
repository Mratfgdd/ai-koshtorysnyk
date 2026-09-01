"""Database session management and schema bootstrap."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_settings = get_settings()

engine: Engine = create_engine(
    _settings.sqlalchemy_url,
    future=True,
    echo=False,
    connect_args={"check_same_thread": False} if _settings.sqlalchemy_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
    """WAL keeps reads working while a long analysis writes progress rows."""
    if not _settings.sqlalchemy_url.startswith("sqlite"):
        return
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db(seed: bool = True) -> None:
    """Create tables, the catalog index, and load the seed on a fresh database."""
    Base.metadata.create_all(engine)
    _create_fts(engine)
    if seed:
        seed_catalog()


SEED_FILE = Path(__file__).resolve().parent / "data" / "seed" / "catalog.json"


def seed_catalog() -> int:
    """Load the bundled price base when the catalog is empty.

    A deployment has no copy of the client's workbook, so without this a fresh
    instance would come up with no prices at all. Never overwrites an existing
    catalog -- a real import always wins.
    """
    from .models import CatalogItem

    if not SEED_FILE.exists():
        return 0
    session = SessionLocal()
    try:
        if session.query(CatalogItem).count():
            return 0
        rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        for row in rows:
            updated = row.pop("price_updated_at", None)
            item = CatalogItem(**row, source_file=str(SEED_FILE), active=True)
            if updated:
                try:
                    item.price_updated_at = dt.datetime.fromisoformat(updated)
                except ValueError:
                    pass
            session.add(item)
        session.commit()
        rebuild_fts(session)
        return len(rows)
    except Exception:  # pragma: no cover - a bad seed must not block startup
        session.rollback()
        return 0
    finally:
        session.close()


FTS_TABLE = "catalog_fts"


def _create_fts(bind: Engine) -> None:
    """Full-text index over catalog names.

    Retrieval, not a giant prompt: product lookup must never mean shipping
    thousands of catalog rows to the model.
    """
    if not _settings.sqlalchemy_url.startswith("sqlite"):
        return
    with bind.begin() as conn:
        try:
            conn.execute(
                text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
                    "USING fts5(name, category, unit, item_id UNINDEXED, tokenize='unicode61')"
                )
            )
        except Exception:  # pragma: no cover - FTS5 unavailable in this build
            # rapidfuzz still provides matching; search just gets slower.
            pass


def rebuild_fts(session: Session) -> int:
    """Repopulate the FTS index from ``catalog_items``. Returns the row count."""
    if not _settings.sqlalchemy_url.startswith("sqlite"):
        return 0
    try:
        session.execute(text(f"DELETE FROM {FTS_TABLE}"))
        session.execute(
            text(
                f"INSERT INTO {FTS_TABLE}(name, category, unit, item_id) "
                "SELECT name, category, unit, id FROM catalog_items WHERE active = 1"
            )
        )
        session.commit()
        return int(session.execute(text(f"SELECT count(*) FROM {FTS_TABLE}")).scalar() or 0)
    except Exception:  # pragma: no cover
        session.rollback()
        return 0


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts and background work."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
