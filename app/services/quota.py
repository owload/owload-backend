"""The limits on what is written (owload-docs/decisions/0036).

Two limits apply to a write, whoever makes it:

* the drive's own limit, set by its owner (`drives.storage_limit_bytes`; none by default): the most the drive may store;
* the owner's limit: the plan's figure (`free_plan_storage_bytes`) for everything the owner has in Owload's own storage (the S3 presets),
  summed over the drives they own. It applies to a write that goes to a preset; a drive that is only on the owner's own S3 is not held by it.

A drive without a limit of its own is held by the owner's limit alone.

The check is made when an upload starts (`POST /sessions`), against what the storage holds, counted again if the last count is not fresh,
plus the room that the uploads still going on have reserved and not written yet. It is not made again for each block, so what is
written during the few seconds between two counts, or by uploads that start at the same moment, can go a little over; and a limit that
is lowered below what is stored stops new writes, it does not remove anything. Nothing here reads or sees what is stored: only its size.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Drive, DriveStorageTarget, SessionStatus, StorageTargetStatus, UploadSession
from app.services.stored_bytes import count_now, storage_usage_of

logger = logging.getLogger(__name__)

STORAGE_LIMIT_EXCEEDED = "STORAGE_LIMIT_EXCEEDED"
# A count younger than this is used as it is; an older one (or none) is made again before a write is let through.
COUNT_FRESH_SECONDS = 15.0

DRIVE_FULL_MESSAGE = "This drive has reached its storage limit."
OWNER_FULL_MESSAGE = "The storage of the drive's owner is full."
OWNER_FULL_MESSAGE_FOR_OWNER = "Your storage is full."


@dataclass(frozen=True)
class Verdict:
    ok: bool
    scope: str | None = None  # "drive" or "plan" when not ok
    limit: int | None = None
    used: int | None = None


def _too_old(drive: Drive, now: datetime) -> bool:
    return drive.stored_bytes_at is None or (now - drive.stored_bytes_at) > timedelta(seconds=COUNT_FRESH_SECONDS)


async def _preset_targets_per_drive(db: AsyncSession, drive_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = (
        await db.execute(
            select(DriveStorageTarget.drive_id, func.count())
            .where(
                DriveStorageTarget.drive_id.in_(drive_ids),
                DriveStorageTarget.preset_id.is_not(None),
                DriveStorageTarget.status.in_([StorageTargetStatus.ACTIVE, StorageTargetStatus.PROVISIONING]),
            )
            .group_by(DriveStorageTarget.drive_id)
        )
    ).all()
    return {drive_id: count for drive_id, count in rows}


async def _unwritten_by_drive(db: AsyncSession, drive_ids: list[uuid.UUID], settings: Settings, now: datetime) -> dict[uuid.UUID, int]:
    """The room that uploads still going on have reserved on each drive and not written yet."""
    since = now - timedelta(hours=settings.upload_reservation_hours)
    rows = (
        await db.execute(
            select(UploadSession.drive_id, func.coalesce(func.sum(UploadSession.byte_length - UploadSession.written_bytes), 0))
            .where(
                UploadSession.drive_id.in_(drive_ids),
                UploadSession.status == SessionStatus.ACTIVE,
                UploadSession.created_at > since,
            )
            .group_by(UploadSession.drive_id)
        )
    ).all()
    return {drive_id: int(total) for drive_id, total in rows}


async def _master_bytes(db: AsyncSession, drive: Drive) -> int:
    """What the drive's master holds, counted again if the last count is not fresh (the last number if it cannot be counted)."""
    now = datetime.now(timezone.utc)
    if _too_old(drive, now):
        if not await count_now(drive.id):
            logger.warning("Could not count drive %s before a write; the last count is used", drive.id)
        await db.refresh(drive)
    return drive.stored_bytes or 0


async def judge_write(db: AsyncSession, drive: Drive, byte_length: int, settings: Settings, copies_of_it: int | None = None) -> Verdict:
    """Whether `byte_length` more bytes may be written to the drive now. `copies_of_it` is how many of the drive's storages are presets and
    would each hold them (by default, those it has now)."""
    if byte_length <= 0:
        return Verdict(True)
    now = datetime.now(timezone.utc)
    presets = await _preset_targets_per_drive(db, [drive.id])
    on_presets = presets.get(drive.id, 0) if copies_of_it is None else copies_of_it
    if drive.storage_limit_bytes is None and on_presets == 0:
        return Verdict(True)  # nothing limits it: no limit of its own, and not in Owload's storage

    master = await _master_bytes(db, drive)
    unwritten = (await _unwritten_by_drive(db, [drive.id], settings, now)).get(drive.id, 0)

    if drive.storage_limit_bytes is not None and master + unwritten + byte_length > drive.storage_limit_bytes:
        return Verdict(False, "drive", drive.storage_limit_bytes, master + unwritten)

    if on_presets > 0:
        usage = await storage_usage_of(db, drive.owner_user_id)
        owned = list((await db.execute(select(Drive.id).where(Drive.owner_user_id == drive.owner_user_id))).scalars().all())
        per_drive = await _preset_targets_per_drive(db, owned)
        waiting = await _unwritten_by_drive(db, owned, settings, now)
        in_flight = sum(room * per_drive.get(drive_id, 0) for drive_id, room in waiting.items())
        used = usage.preset_bytes + in_flight
        if used + byte_length * on_presets > settings.free_plan_storage_bytes:
            return Verdict(False, "plan", settings.free_plan_storage_bytes, used)
    return Verdict(True)


async def ensure_room_for_copy(db: AsyncSession, drive: Drive, user_id: str, settings: Settings) -> None:
    """A new preset storage is added to the drive and will be given a copy of what the drive holds: the owner's limit must have room for
    that copy (the drive's own limit is about what it holds, which does not change)."""
    master = await _master_bytes(db, drive)
    if master <= 0:
        return
    usage = await storage_usage_of(db, drive.owner_user_id)
    owned = list((await db.execute(select(Drive.id).where(Drive.owner_user_id == drive.owner_user_id))).scalars().all())
    per_drive = await _preset_targets_per_drive(db, owned)
    waiting = await _unwritten_by_drive(db, owned, settings, datetime.now(timezone.utc))
    used = usage.preset_bytes + sum(room * per_drive.get(drive_id, 0) for drive_id, room in waiting.items())
    if used + master > settings.free_plan_storage_bytes:
        raise refusal(Verdict(False, "plan", settings.free_plan_storage_bytes, used), is_owner=user_id == drive.owner_user_id)


def refusal(verdict: Verdict, is_owner: bool) -> HTTPException:
    """The 413 for a write that does not fit. The figures are only given to the owner: somebody else who writes to the drive is told
    that it is full, not how much the owner has."""
    if verdict.scope == "drive":
        message = DRIVE_FULL_MESSAGE
    else:
        message = OWNER_FULL_MESSAGE_FOR_OWNER if is_owner else OWNER_FULL_MESSAGE
    detail: dict = {"code": STORAGE_LIMIT_EXCEEDED, "scope": verdict.scope, "message": message}
    if is_owner or verdict.scope == "drive":
        detail["limitBytes"] = verdict.limit
        detail["usedBytes"] = verdict.used
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail)


async def ensure_room(db: AsyncSession, drive: Drive, user_id: str, byte_length: int, settings: Settings, copies_of_it: int | None = None) -> None:
    verdict = await judge_write(db, drive, byte_length, settings, copies_of_it)
    if not verdict.ok:
        raise refusal(verdict, is_owner=user_id == drive.owner_user_id)


