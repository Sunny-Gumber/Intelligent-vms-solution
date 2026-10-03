from datetime import datetime, timezone
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import DateTime


class Base(DeclarativeBase):
    """Provide the declarative SQLAlchemy base for VMS ORM entities.

    ORM model classes inherit from this base to participate in shared metadata.
    Normal declaration does not return a value or raise a domain-specific
    exception.
    """

    pass


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp.

    Returns:
        Current UTC datetime with timezone information.
    """
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Add automatically managed creation and update timestamps to ORM models.

    Attributes:
        created_at: UTC timestamp assigned when the row is created.
        updated_at: UTC timestamp assigned on creation and updated on writes.

    Raises:
        No domain-specific exception is raised by the mixin itself.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
