import asyncio
import logging
import os

from app.services.placement import run_placement_once

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("placement-controller")
INTERVAL = max(2.0, float(os.getenv("PLACEMENT_INTERVAL_SECONDS", "10")))


async def main():
    """Run periodic bounded placement-controller iterations indefinitely.

    Returns:
        None under normal operation; the coroutine runs until cancelled.
    """
    while True:
        try:
            result = await run_placement_once()
            log.info(
                "placement_run scanned=%s moved=%s unplaced=%s cursor=%s",
                result["scanned"], result["moved"], result["unplaced"], result["cursor"],
            )
        except Exception:
            log.exception("placement_run_failed")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
