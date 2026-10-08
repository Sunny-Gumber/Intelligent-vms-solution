import asyncio
import logging
import os

from app.core.config import settings
from app.core.placement_renewal import (
    PlacementRenewalBudgetError,
    assert_placement_renewal_budget,
    resolved_placement_interval_seconds,
)
from app.services.placement import run_placement_once

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("placement-controller")


def validate_placement_renewal_budget() -> None:
    """Refuse to start when renewal cannot visit every configured owner before the lease expires.

    Returns:
        None when the configured cycle is strictly shorter than the lease.

    Raises:
        PlacementRenewalBudgetError: When the ceiling, batch and interval cannot
            meet the lease. The lease is not shortened to force a fit.
    """
    assert_placement_renewal_budget(
        max_assignments=settings.placement_renewal_max_assignments,
        batch_size=settings.placement_renewal_batch_size,
        interval_seconds=settings.placement_interval_seconds,
        lease_seconds=settings.placement_lease_seconds,
    )


async def main():
    """Run periodic bounded placement-controller iterations indefinitely.

    Startup rejects a renewal budget that cannot refresh every configured
    owner before the lease expires. A later population that exceeds the same
    ceiling stops the process instead of continuing a scan that would fence
    healthy owners.

    Returns:
        None under normal operation; the coroutine runs until cancelled.

    Raises:
        PlacementRenewalBudgetError: The renewal budget does not hold at startup
            or a run observes more active assignments than the ceiling.
    """
    validate_placement_renewal_budget()
    while True:
        try:
            result = await run_placement_once()
            log.info(
                "placement_run scanned=%s moved=%s unplaced=%s renewed=%s cursor=%s",
                result["scanned"],
                result["moved"],
                result["unplaced"],
                result.get("renewed", 0),
                result["cursor"],
            )
        except PlacementRenewalBudgetError:
            log.exception("placement_renewal_budget_unsatisfiable")
            raise
        except Exception:
            log.exception("placement_run_failed")
        await asyncio.sleep(
            resolved_placement_interval_seconds(settings.placement_interval_seconds)
        )


if __name__ == "__main__":
    asyncio.run(main())
