from datetime import datetime, timezone


FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    """Datetime replacement that returns the shared deterministic test instant."""

    @classmethod
    def now(cls, tz=None):
        """Return the fixed test instant in the requested timezone.

        Args:
            tz: Optional timezone requested by production code.

        Returns:
            The deterministic test instant, naive only when no timezone is supplied.
        """
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)
