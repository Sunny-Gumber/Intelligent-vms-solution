"""Declarative base and portable column types.

Portability note (ADR-0004): PostgreSQL is the supported production database;
SQLite is supported for developer checkouts and the automated test suite only.
One schema definition serves both via:
  * String(32) hex UUID primary keys generated in Python (no DB extension)
  * JSON mapped to JSONB on PostgreSQL through a dialect variant
  * UtcDateTime, which refuses naive values and always returns aware UTC
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, JSON, MetaData, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.timeutil import ensure_utc, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


JSONType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator):
    """DateTime that normalises to aware UTC on the way in and out."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        return None if value is None else ensure_utc(value)

    def process_result_value(self, value, dialect):  # noqa: ANN001
        return None if value is None else ensure_utc(value)


def new_id() -> str:
    return uuid.uuid4().hex


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False)


def as_dict(obj: Any) -> dict[str, Any]:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
