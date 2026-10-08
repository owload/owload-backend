import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_accessible_drive_or_404
from app.api.errors import storage_write_failed
from app.core.security import CurrentUser, get_current_user
from app.db.models import (
    Drive,
    DriveAction,
    DriveActionLog,
    DriveStorageTarget,
    S3Preset,
    S3Tier,
    StorageTargetRole,
    StorageTargetStatus,
)
from app.db.session import get_db
from app.schemas.drive import CreateDriveIn, DeleteDriveIn, DriveInfoOut
from app.schemas.drive_action import DriveActionLogOut
from app.services.activity import (
    DRIVE_CREATED,
    DRIVE_DELETED,
    ClientContext,
    get_client_context,
    record_event,
    session_key,
)
from app.services.audit import log_action
from app.services.secrets import encrypt_secret
from app.services.storage import BlockStorage, BlockWriteError, get_storage
from app.services.storage_targets import (
    get_active_targets,
    get_or_create_legacy_global_preset,
    resolve_target,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drives", tags=["drives"])


def _to_drive_info(drive: Drive) -> DriveInfoOut:
    return DriveInfoOut(
        id=str(drive.id),
        owner_user_id=drive.owner_user_id,
        title=drive.title,
        acl=drive.acl,
        created_timestamp=int(drive.created_at.timestamp() * 1000),
        key_nonce=drive.key_nonce,
        counter_nonce=drive.counter_nonce,
    )


async def _initial_target_kwargs(db: AsyncSession, payload: CreateDriveIn) -> dict:
    """Resolves the drive's initial (always MASTER) target from the caller's
    choice. Master must be hot tier (owload-docs/decisions/0011) — cold presets
    exist only for slaves added later. custom_config is always treated as hot.
    Neither field given falls back to the shared legacy-global preset, so
    older frontend builds that don't send either yet keep working unchanged."""
    if payload.custom_config is not None:
        cfg = payload.custom_config
        # Only "custom_config" is passed to the DriveStorageTarget constructor
        # below — never an explicit "preset_id": None alongside it. JSONB
        # columns treat an explicit Python None as the JSON literal `null`,
        # not SQL NULL, which would trip the "exactly one of the two is set"
        # CHECK constraint. Leaving the other field unset gives real NULL.
        return {
            "custom_config": {
                "endpoint_url": cfg.endpoint_url,
                "region": cfg.region,
                "bucket": cfg.bucket,
                "access_key": cfg.access_key,
                "secret_key": encrypt_secret(cfg.secret_key),
                "use_ssl": cfg.use_ssl,
            },
        }
    if payload.preset_id is not None:
        preset = await db.get(S3Preset, payload.preset_id)
        if preset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Storage preset not found")
        if preset.tier != S3Tier.HOT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "The drive's initial (master) target must use a hot-tier preset"
            )
        return {"preset_id": preset.id}
    preset = await get_or_create_legacy_global_preset(db)
    return {"preset_id": preset.id}


@router.post("", response_model=DriveInfoOut, status_code=status.HTTP_201_CREATED)
async def create_drive(
    payload: CreateDriveIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> DriveInfoOut:
    target_kwargs = await _initial_target_kwargs(db, payload)

    drive = Drive(
        owner_user_id=user.sub,
        title=payload.title,
        key_nonce=payload.key_nonce,
        counter_nonce=payload.counter_nonce,
        acl={},
    )
    db.add(drive)
    await db.flush()  # sends the INSERT and populates drive.created_at via RETURNING

    db.add(
        DriveStorageTarget(
            drive_id=drive.id,
            role=StorageTargetRole.MASTER,
            status=StorageTargetStatus.ACTIVE,
            **target_kwargs,
        )
    )
    await db.flush()
    targets = await get_active_targets(db, drive.id)

    # Recovery backup in S3. Written before commit: if this fails, the
    # exception propagates and the uncommitted INSERT is rolled back when the
    # session closes, so a drive never exists in Postgres without its backup.
    try:
        await storage.write_drive_meta(
            targets, drive.id, drive.owner_user_id, drive.title, drive.key_nonce, drive.counter_nonce, drive.created_at
        )
    except BlockWriteError as exc:
        raise storage_write_failed(exc) from exc

    log_action(db, drive.id, user.sub, DriveAction.CREATE_DRIVE, title=drive.title)
    record_event(db, user.sub, DRIVE_CREATED, ctx, session_id=session_key(user, ctx), driveId=str(drive.id), title=drive.title)
    await db.commit()
    return _to_drive_info(drive)


@router.get("", response_model=list[DriveInfoOut])
async def get_accessible_drives(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DriveInfoOut]:
    result = await db.execute(
        select(Drive).where(or_(Drive.owner_user_id == user.sub, Drive.acl.has_key(user.sub)))
    )
    return [_to_drive_info(d) for d in result.scalars().all()]


@router.get("/{drive_id}", response_model=DriveInfoOut)
async def get_drive_info(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DriveInfoOut:
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    return _to_drive_info(drive)


@router.delete("/{drive_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_drive(
    drive_id: uuid.UUID,
    payload: DeleteDriveIn,
    force: bool = Query(
        False,
        description="Delete the drive even if some target's data couldn't be purged "
        "(e.g. its S3 is unreachable) — leaves orphaned data behind on that storage",
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Deletes a drive outright — owner only, not just anyone in the ACL
    (owload-docs/decisions/0013): far more destructive than the read/write
    access ACL membership grants. Preset targets always get their data
    wiped; each custom target needs the owner's explicit deleteData
    choice, since a custom target is the owner's own S3 infrastructure —
    the app has no business deciding to erase it without being told to."""
    drive = await db.get(Drive, drive_id)
    if drive is None or (drive.owner_user_id != user.sub and user.sub not in drive.acl):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drive not found")
    if drive.owner_user_id != user.sub:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the drive owner can delete it")

    targets = (
        await db.execute(
            select(DriveStorageTarget)
            .options(selectinload(DriveStorageTarget.preset))
            .where(DriveStorageTarget.drive_id == drive_id)
        )
    ).scalars().all()

    custom_target_ids = {t.id for t in targets if t.custom_config is not None}
    decisions = {d.target_id: d.delete_data for d in payload.custom_target_decisions}
    missing = custom_target_ids - decisions.keys()
    extra = decisions.keys() - custom_target_ids
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing a deleteData decision for custom target(s): {', '.join(str(i) for i in missing)}")
        if extra:
            parts.append(
                f"decision given for target(s) that aren't this drive's custom targets: "
                f"{', '.join(str(i) for i in extra)}"
            )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(parts))

    for target in targets:
        if target.preset_id is not None or decisions[target.id]:
            try:
                await storage.purge_target_data(resolve_target(target), drive_id)
            except Exception as exc:
                # Broad on purpose: a totally unreachable custom S3 (host down,
                # credentials revoked) doesn't raise BlockDeleteError — that
                # only covers a reachable S3 reporting per-key failures — and
                # without `force` we'd otherwise be unable to ever delete a
                # drive whose owner-managed storage has simply gone away.
                if not force:
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY,
                        f"Failed to delete data for target {target.id} ({exc}); the drive was not "
                        f"deleted — retry once the target is reachable again, or pass force=true to "
                        f"delete the drive anyway (data may be left behind on that target)",
                    ) from exc
                logger.error(
                    "force-deleting drive %s despite failed purge on target %s: %s",
                    drive_id, target.id, exc, exc_info=True,
                )

    title = drive.title
    await db.delete(drive)
    # The drive's own action log goes with it (cascade); the account's event outlives it.
    record_event(db, user.sub, DRIVE_DELETED, ctx, session_id=session_key(user, ctx), driveId=str(drive_id), title=title)
    await db.commit()


@router.get("/{drive_id}/actions", response_model=list[DriveActionLogOut])
async def get_drive_actions(
    drive_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(
        None, description="seq cursor from the last item of the previous page — omit for the first page"
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DriveActionLogOut]:
    """Audit trail for a drive, most recent first. Keyset-paginated on `seq`
    (not OFFSET): a row inserted between two page fetches can never shift
    which rows the next page returns, unlike OFFSET, which counts position
    among whatever's "live" at the moment of each query."""
    await get_accessible_drive_or_404(db, drive_id, user.sub)
    stmt = select(DriveActionLog).where(DriveActionLog.drive_id == drive_id)
    if before is not None:
        stmt = stmt.where(DriveActionLog.seq < before)
    stmt = stmt.order_by(desc(DriveActionLog.seq)).limit(limit)

    result = await db.execute(stmt)
    return [
        DriveActionLogOut(
            id=str(row.id),
            drive_id=str(row.drive_id),
            user_id=row.user_id,
            action=row.action,
            attributes=row.attributes,
            timestamp=int(row.created_at.timestamp() * 1000),
            seq=row.seq,
        )
        for row in result.scalars().all()
    ]
