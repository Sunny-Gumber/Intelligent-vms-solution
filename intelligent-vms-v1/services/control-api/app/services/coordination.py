from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


# One transaction-scoped PostgreSQL advisory lock serializes assignment ownership
# mutation with any control-plane operation that can start/reconfigure distributed
# media/recording work. This closes the "old reconciler request races failover"
# window without introducing a second ownership source of truth.
PLACEMENT_EXECUTION_LOCK_KEY = 0x564D5307

# An authority change waits at most this long for a placement transaction that
# already holds the fence. The wait is bounded so a stuck holder cannot pin the
# request. Callers acquire this lock before they lock site, policy, node, or
# assignment rows. Renewal acquires it first, then locks those rows.
PLACEMENT_AUTHORITY_LOCK_WAIT_SECONDS = 30


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


def _lock_not_available(error: BaseException) -> bool:
    orig = getattr(error, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return state == "55P03"


async def await_placement_execution_lock(
    session,
    *,
    timeout_seconds: float = PLACEMENT_AUTHORITY_LOCK_WAIT_SECONDS,
) -> None:
    """Wait for the placement fence, then hold it until this transaction ends.

    Site-region, node-region, and policy writes call this before they lock
    their own rows. A renewal that already holds the fence keeps its authority
    locks until it commits, so this wait cannot commit a region or policy
    change underneath that renewal. The lock timeout bounds the wait.

    Args:
        session: SQLAlchemy session that will commit the authority change.
        timeout_seconds: Maximum seconds to wait for the fence. The default is
            PLACEMENT_AUTHORITY_LOCK_WAIT_SECONDS.

    Returns:
        None after the fence is held for this transaction. Non-PostgreSQL
        backends have no advisory lock and return immediately.

    Raises:
        PlacementExecutionBusy: The fence was still held when the lock timeout
            expired.
        Exception: Database failures propagate to the caller.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    timeout_ms = max(1, int(float(timeout_seconds) * 1000))
    await session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": str(timeout_ms)},
    )
    try:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": PLACEMENT_EXECUTION_LOCK_KEY},
        )
    except DBAPIError as error:
        if _lock_not_available(error):
            raise PlacementExecutionBusy("placement execution fence is busy") from error
        raise
