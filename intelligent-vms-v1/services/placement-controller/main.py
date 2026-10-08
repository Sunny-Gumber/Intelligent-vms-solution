import asyncio
import logging
import os

from app.core.config import settings
from app.core.placement_renewal import (
    PlacementRenewalBudgetError,
    assert_settings_renewal_budget,
    resolved_placement_interval_seconds,
)
from app.services.placement import run_placement_once

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("placement-controller")


def validate_placement_renewal_budget() -> None:
    """Refuse to start when the conservative renewal budget does not fit the lease.

    Returns:
        None when pages, missed locks, fence poll, clock skew and the safety
        margin are strictly inside the lease.

    Raises:
        PlacementRenewalBudgetError: When the configured budget cannot refresh
            every owner before lease expiry. The lease is not shortened.
    """
    assert_settings_renewal_budget(settings)


async def main():
    """Run periodic bounded placement-controller iterations indefinitely.

    Startup rejects a renewal budget that cannot refresh every configured
    owner before the lease expires. A live population above that ceiling skips
    renewal for the run and leaves scan and failover running. A missed
    execution lock is retried inside the run, with a short backoff, before
    this cycle sleeps out the full interval.

    Returns:
        None under normal operation; the coroutine runs until cancelled.

    Raises:
        PlacementRenewalBudgetError: The configured renewal budget does not
            hold at startup or on a run. A population above the ceiling does
            not raise.
    """
    validate_placement_renewal_budget()
    while True:
        try:
            result = await run_placement_once()
            if result.get("renewal_budget_exceeded"):
                log.critical(
                    "placement_renewal_budget_exceeded scanned=%s moved=%s renewed=%s",
                    result.get("scanned"),
                    result.get("moved"),
                    result.get("renewed"),
                )
            log.info(
                "placement_run scanned=%s moved=%s unplaced=%s renewed=%s "
                "renewal_budget_exceeded=%s cursor=%s",
                result["scanned"],
                result["moved"],
                result["unplaced"],
                result.get("renewed", 0),
                result.get("renewal_budget_exceeded", False),
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
