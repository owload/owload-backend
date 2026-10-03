"""Runs the ops/ -> ops-batches/ compaction pass (see app/services/compaction.py)
on every storage target of every drive.

Usage:
    python -m app.compaction_worker                       # single pass, then exit
    python -m app.compaction_worker --loop                # repeats forever
    python -m app.compaction_worker --loop --interval 900  # every 15 minutes

Needs Postgres: drive_storage_targets tells it which S3 targets each drive has
and how to connect to them (like app/backfill_worker.py). The compaction work
itself only touches S3, so it can run as a plain cron job or a small
long-lived worker container next to the API.
"""

import argparse
import asyncio
import logging

from app.db.session import async_session_factory
from app.services.compaction import get_compaction_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("compaction_worker")


async def run_once() -> None:
    service = get_compaction_service()
    async with async_session_factory() as db:
        results = await service.compact_all_drives(db)
    batched = sum(r["batched_chunks"] for r in results)
    cleaned = sum(r["cleaned_up"] for r in results)
    logger.info(
        "compaction pass done: %d (drive, target) pair(s) scanned, %d chunk(s) merged, %d leftover object(s) cleaned",
        len(results),
        batched,
        cleaned,
    )
    for r in results:
        if r["batched_chunks"] or r["cleaned_up"]:
            logger.info("  drive %s target %s: %s", r["drive_id"], r["target_id"], r)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep running, sleeping --interval seconds between passes")
    parser.add_argument("--interval", type=int, default=900, help="seconds between passes when --loop is set (default: 900)")
    args = parser.parse_args()

    if not args.loop:
        await run_once()
        return

    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("compaction pass failed, will retry next interval")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
