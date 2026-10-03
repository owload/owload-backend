"""Copies a drive's existing S3 backlog from its master onto any storage
target still PROVISIONING (added via POST /drives/{driveId}/storage-targets,
epic 0012 task 0005), then flips it to ACTIVE (see app/services/backfill.py).

Usage:
    python -m app.backfill_worker                      # single pass, then exit
    python -m app.backfill_worker --loop                # repeats forever
    python -m app.backfill_worker --loop --interval 60   # every minute

Like app/compaction_worker.py, this needs Postgres — it reads
drive_storage_targets to know which target is PROVISIONING and to resolve
both its and its drive's master's connection details.
"""

import argparse
import asyncio
import logging

from app.db.session import async_session_factory
from app.services.backfill import get_backfill_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_worker")


async def run_once() -> None:
    service = get_backfill_service()
    async with async_session_factory() as db:
        results = await service.backfill_all_pending(db)
    if not results:
        logger.info("backfill pass done: nothing pending")
        return
    logger.info("backfill pass done: %d target(s) completed", len(results))
    for r in results:
        logger.info("  drive %s target %s: %s", r["drive_id"], r["target_id"], r)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep running, sleeping --interval seconds between passes")
    parser.add_argument("--interval", type=int, default=60, help="seconds between passes when --loop is set (default: 60)")
    args = parser.parse_args()

    if not args.loop:
        await run_once()
        return

    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("backfill pass failed, will retry next interval")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
