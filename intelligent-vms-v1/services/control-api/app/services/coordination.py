from sqlalchemy import text


# One transaction-scoped PostgreSQL advisory lock serializes assignment ownership
# mutation with any control-plane operation that can start/reconfigure distributed
# media/recording work. This closes the "old reconciler request races failover"
# window without introducing a second ownership source of truth.
PLACEMENT_EXECUTION_LOCK_KEY = 0x564D5307


async def try_placement_execution_lock(session) -> bool:
    """Try to acquire the transaction-scoped placement execution fence.

    Args:
        session: SQLAlchemy session participating in the ownership mutation.

    Returns:
        True when the advisory lock is acquired or the backend is not PostgreSQL.

    Raises:
        Exception: Database execution failures propagate to the caller.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    result = await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": PLACEMENT_EXECUTION_LOCK_KEY},
    )
    return bool(result.scalar())


class PlacementExecutionBusy(RuntimeError):
    """Raised when placement ownership is changing under the execution fence."""


async def require_placement_execution_lock(session) -> None:
    """Require the placement execution fence before mutating distributed work.

    Args:
        session: SQLAlchemy session participating in the ownership mutation.

    Returns:
        None after the transaction-scoped fence is acquired.

    Raises:
        PlacementExecutionBusy: If another transaction currently owns the fence.
        Exception: Database failures propagate to the caller.
    """
    if not await try_placement_execution_lock(session):
        raise PlacementExecutionBusy("placement execution fence is busy")
