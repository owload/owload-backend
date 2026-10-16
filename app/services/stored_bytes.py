"""How many bytes a drive really stores, for its card, the side panel and the profile page.

The server knows it by listing the blocks of the drive in its master storage. That costs a request per thousand blocks, so the
number is kept on the drive with the time it was counted, and counted again when it is older than a few minutes or when something
was written or freed (`mark_stale`). A list of drives never waits long for it: what is not counted in two seconds goes on in the
background and the next list has it."""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Drive, DriveStorageTarget, StorageTargetRole, StorageTargetStatus
from app.db.session import async_session_factory
from app.services.storage import get_storage
from app.services.storage_targets import resolve_target

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
    """Counts the drive's blocks in each of its storages and keeps the results: in every storage's own row (what the user's usage is added
    up from), and the master's also on the drive (the size of the drive). A storage that cannot be asked keeps its last number; None is
    returned (and the drive stays to be counted again) when the master could not be asked."""
    try:
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(DriveStorageTarget)
                    .options(selectinload(DriveStorageTarget.preset))
                    .where(
                        DriveStorageTarget.drive_id == drive_id,
                        DriveStorageTarget.status.in_([StorageTargetStatus.ACTIVE, StorageTargetStatus.PROVISIONING]),
                    )
                )
            ).scalars().all()
            storage = get_storage()

            async def count_one(row: DriveStorageTarget) -> int | None:
                try:
                    return await storage.sum_stored_bytes(resolve_target(row), drive_id)
                except Exception:  # one storage that is down must not hide the others
                    logger.warning("Could not count drive %s in storage %s", drive_id, row.id, exc_info=True)
                    return None

            totals = await asyncio.gather(*(count_one(row) for row in rows))
            master_total: int | None = None
            for row, total in zip(rows, totals):
                if total is None:
                    continue
                row.stored_bytes = total
                if row.role == StorageTargetRole.MASTER:
                    master_total = total
            if master_total is None:
                await db.commit()
                _failed_at[drive_id] = time.monotonic()
                return None
            await db.execute(update(Drive).where(Drive.id == drive_id).values(stored_bytes=master_total, stored_bytes_at=func.now()))
            await db.commit()
            _failed_at.pop(drive_id, None)
            return master_total
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


@dataclass(frozen=True)
class StorageUsage:
    """What the drives a user owns take in the storages: in Owload's own (the presets) and in the user's own S3 (custom configurations)."""

    preset_bytes: int
    custom_bytes: int
    preset_drives: int
    custom_drives: int
    # False while some storage of some drive has not been counted yet (the numbers are then a part of the whole)
    complete: bool


async def storage_usage_of(db: AsyncSession, user_id: str, wait_seconds: float = WAIT_FOR_COUNT_SECONDS) -> StorageUsage:
    """Adds up, over the drives the user owns, the bytes each of their storages holds. A drive shared with the user is on its owner's
    account and does not count. What is stale is counted again first (see `stored_bytes_of` for how much is waited for)."""
    drives = list((await db.execute(select(Drive).where(Drive.owner_user_id == user_id))).scalars().all())
    if drives:
        await stored_bytes_of(drives, wait_seconds)
    rows = (
        await db.execute(
            select(DriveStorageTarget.drive_id, DriveStorageTarget.preset_id.is_not(None), DriveStorageTarget.stored_bytes)
            .join(Drive, Drive.id == DriveStorageTarget.drive_id)
            .where(
                Drive.owner_user_id == user_id,
                DriveStorageTarget.status.in_([StorageTargetStatus.ACTIVE, StorageTargetStatus.PROVISIONING]),
            )
        )
    ).all()
    preset = custom = 0
    preset_drives: set[uuid.UUID] = set()
    custom_drives: set[uuid.UUID] = set()
    complete = True
    for drive_id, is_preset, stored in rows:
        if stored is None:
            complete = False
            continue
        if is_preset:
            preset += stored
            preset_drives.add(drive_id)
        else:
            custom += stored
            custom_drives.add(drive_id)
    return StorageUsage(preset, custom, len(preset_drives), len(custom_drives), complete)
