"""How many bytes a drive really stores, for its card, the side panel and the profile page.

The server knows it by listing the blocks of the drive in its master storage. That costs a request per thousand blocks, so the
number is kept on the drive with the time it was counted, and counted again when it is older than a few minutes or when something
was written or freed (`mark_stale`). A list of drives never waits long for it: what is not counted in two seconds goes on in the
background and the next list has it."""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update

from app.db.models import Drive
from app.db.session import async_session_factory
from app.services.storage import get_storage
from app.services.storage_targets import get_active_targets, get_master

logger = logging.getLogger(__name__)

STORED_BYTES_TTL = timedelta(minutes=5)
WAIT_FOR_COUNT_SECONDS = 2.0
# A drive whose storage could not be asked is left alone for a while instead of being tried on every list of drives.
RETRY_AFTER_FAILURE_SECONDS = 60.0
# A list of drives starts the count of at most this many of them (the newest); the rest are counted by later lists. A user with many
# drives does not open that many connections to the storages at once.
MAX_COUNTS_PER_LIST = 8

_counting: dict[uuid.UUID, "asyncio.Task[int | None]"] = {}
_failed_at: dict[uuid.UUID, float] = {}


def is_fresh(drive: Drive, now: datetime | None = None) -> bool:
    if drive.stored_bytes is None or drive.stored_bytes_at is None:
        return False
    return (now or datetime.now(timezone.utc)) - drive.stored_bytes_at < STORED_BYTES_TTL


async def _count(drive_id: uuid.UUID) -> int | None:
    """Counts the drive's blocks in its master storage and keeps the result; None if the storage could not be asked."""
    try:
        async with async_session_factory() as db:
            master = get_master(await get_active_targets(db, drive_id))
            total = await get_storage().sum_stored_bytes(master, drive_id)
            await db.execute(update(Drive).where(Drive.id == drive_id).values(stored_bytes=total, stored_bytes_at=func.now()))
            await db.commit()
            _failed_at.pop(drive_id, None)
            return total
    except Exception:  # a storage that is down must not break the list of drives
        _failed_at[drive_id] = time.monotonic()
        logger.warning("Could not count the stored bytes of drive %s", drive_id, exc_info=True)
        return None
    finally:
        _counting.pop(drive_id, None)


def _start(drive_id: uuid.UUID) -> "asyncio.Task[int | None]":
    task = _counting.get(drive_id)
    if task is None or task.done():
        task = asyncio.get_running_loop().create_task(_count(drive_id))
        _counting[drive_id] = task
    return task


async def stored_bytes_of(drives: list[Drive], wait_seconds: float = WAIT_FOR_COUNT_SECONDS) -> dict[uuid.UUID, int | None]:
    """The stored bytes of each drive: the kept number when it is fresh, else a new count, waited for at most `wait_seconds` in all.
    A drive that is not counted in time gets its last number (or None if it never had one) and the count goes on without waiting."""
    result: dict[uuid.UUID, int | None] = {d.id: d.stored_bytes for d in drives}
    now = time.monotonic()
    stale = [d for d in drives if not is_fresh(d) and now - _failed_at.get(d.id, -RETRY_AFTER_FAILURE_SECONDS) >= RETRY_AFTER_FAILURE_SECONDS]
    if not stale:
        return result
    # the newest drives first: they are the ones being used, and a drive whose storage is down must not hold them up
    stale.sort(key=lambda d: d.created_at, reverse=True)
    tasks = {d.id: _start(d.id) for d in stale[:MAX_COUNTS_PER_LIST]}
    done, _ = await asyncio.wait(list(tasks.values()), timeout=wait_seconds)
    for drive_id, task in tasks.items():
        if task in done:
            counted = task.result()
            if counted is not None:
                result[drive_id] = counted
    return result


def mark_stale(drive: Drive) -> None:
    """Something was written or freed: the number is to be counted again the next time somebody asks. Commit with the caller's change."""
    drive.stored_bytes_at = None
