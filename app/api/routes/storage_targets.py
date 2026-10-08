import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_accessible_drive_or_404
from app.core.security import CurrentUser, get_current_user
from app.db.models import (
    DriveStorageTarget,
    S3Preset,
    S3Tier,
    StorageTargetRole,
    StorageTargetStatus,
)
from app.db.session import get_db
from app.schemas.drive import CustomStorageConfigIn
from app.schemas.storage_target import (
    AddStorageTargetIn,
    ConnectionTestOut,
    StorageTargetOut,
)
from app.services.activity import (
    STORAGE_TARGET_ADDED,
    STORAGE_TARGET_MAIN_CHANGED,
    STORAGE_TARGET_REMOVED,
    ClientContext,
    get_client_context,
    record_event,
    session_key,
)
from app.services.backfill import BackfillService, get_backfill_service
from app.services.secrets import encrypt_secret
from app.services.storage import BlockStorage, get_storage
from app.services.storage_targets import ResolvedTarget, resolve_target

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drives/{drive_id}/storage-targets", tags=["storage-targets"])

# Not drive-scoped — tests a not-yet-saved custom config before it's ever
# attached to a drive, so there's no {drive_id} to nest it under.
test_router = APIRouter(prefix="/storage-targets", tags=["storage-targets"])

MAX_SLAVE_TARGETS = 2


def _to_target_out(target: DriveStorageTarget, progress: dict | None = None) -> StorageTargetOut:
    backfill_copied = progress["copied"] if progress else None
    backfill_total = progress["total"] if progress else None
    if target.preset_id is not None:
        preset = target.preset
        return StorageTargetOut(
            id=str(target.id),
            role=target.role,
            status=target.status,
            tier=preset.tier.lower(),
            preset_id=str(target.preset_id),
            preset_label=preset.label,
            is_custom=False,
            backfill_copied=backfill_copied,
            backfill_total=backfill_total,
        )
    cfg = target.custom_config
    return StorageTargetOut(
        id=str(target.id),
        role=target.role,
        status=target.status,
        tier=S3Tier.HOT.lower(),  # custom targets are always hot, see decisions/0011
        is_custom=True,
        custom_endpoint_url=cfg.get("endpoint_url"),
        custom_bucket=cfg["bucket"],
        backfill_copied=backfill_copied,
        backfill_total=backfill_total,
    )


def _target_location(target: DriveStorageTarget) -> tuple[str | None, str]:
    """A target's physical (endpoint_url, bucket) — two targets pointing at
    the same one aren't independent copies of anything, whatever the data
    model thinks (owload-docs/epics/0012 task 0011): removing one would
    actually delete the other's "live" data too."""
    if target.preset_id is not None:
        preset = target.preset
        return (preset.endpoint_url, preset.bucket)
    cfg = target.custom_config
    return (cfg.get("endpoint_url"), cfg["bucket"])


async def _get_target_or_404(db: AsyncSession, drive_id: uuid.UUID, target_id: uuid.UUID) -> DriveStorageTarget:
    stmt = (
        select(DriveStorageTarget)
        .options(selectinload(DriveStorageTarget.preset))
        .where(DriveStorageTarget.id == target_id, DriveStorageTarget.drive_id == drive_id)
    )
    target = (await db.execute(stmt)).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Storage target not found")
    return target


@router.get("", response_model=list[StorageTargetOut])
async def list_storage_targets(
    drive_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    backfill: BackfillService = Depends(get_backfill_service),
) -> list[StorageTargetOut]:
    await get_accessible_drive_or_404(db, drive_id, user.sub)
    stmt = (
        select(DriveStorageTarget)
        .options(selectinload(DriveStorageTarget.preset))
        .where(DriveStorageTarget.drive_id == drive_id)
        .order_by(DriveStorageTarget.created_at)
    )
    targets = (await db.execute(stmt)).scalars().all()

    result = []
    for t in targets:
        progress = None
        if t.status == StorageTargetStatus.PROVISIONING:
            try:
                progress = await backfill.progress(db, t)
            except Exception:
                pass  # best-effort — a listing hiccup shouldn't break the whole page
        result.append(_to_target_out(t, progress))
    return result


@router.post("", response_model=StorageTargetOut, status_code=status.HTTP_201_CREATED)
async def add_storage_target(
    drive_id: uuid.UUID,
    payload: AddStorageTargetIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> StorageTargetOut:
    """Adds a slave target. It's already included in fan-out writes from
    this point on (see get_active_targets) regardless of status — copying
    any *historical* backlog it still needs is a separate worker's job
    (owload-docs/epics/0013-s3-target-backfill-worker, not built yet), so a
    target normally starts `PROVISIONING` and stays that way forever until
    that worker exists. But a drive with no data or ops history yet has
    nothing to backfill in the first place — master and a brand-new slave
    are already trivially byte-identical (both empty) — so that one case
    can safely go straight to ACTIVE without waiting on epic 0013 at all."""
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    initial_status = (
        StorageTargetStatus.ACTIVE
        if drive.data_length == 0 and len(drive.ops_data) == 0
        else StorageTargetStatus.PROVISIONING
    )

    existing_targets = (
        await db.execute(
            select(DriveStorageTarget)
            .options(selectinload(DriveStorageTarget.preset))
            .where(
                DriveStorageTarget.drive_id == drive_id,
                DriveStorageTarget.status != StorageTargetStatus.REMOVING,
            )
        )
    ).scalars().all()

    existing_slave_count = sum(1 for t in existing_targets if t.role == StorageTargetRole.SLAVE)
    if existing_slave_count >= MAX_SLAVE_TARGETS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"A drive can have at most {MAX_SLAVE_TARGETS} slave targets"
        )

    preset: S3Preset | None = None
    if payload.custom_config is not None:
        new_location = (payload.custom_config.endpoint_url, payload.custom_config.bucket)
    else:
        preset = await db.get(S3Preset, payload.preset_id)
        if preset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Storage preset not found")
        new_location = (preset.endpoint_url, preset.bucket)

    # Compare against every existing target (MASTER included, not just other
    # slaves) — two targets at the same physical location are one copy of
    # data wearing two rows, not two independent copies (task 0011).
    if any(_target_location(t) == new_location for t in existing_targets):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This drive already has a storage target pointing at the same location",
        )

    if payload.custom_config is not None:
        cfg = payload.custom_config
        target = DriveStorageTarget(
            drive_id=drive_id,
            role=StorageTargetRole.SLAVE,
            status=initial_status,
            custom_config={
                "endpoint_url": cfg.endpoint_url,
                "region": cfg.region,
                "bucket": cfg.bucket,
                "access_key": cfg.access_key,
                "secret_key": encrypt_secret(cfg.secret_key),
                "use_ssl": cfg.use_ssl,
            },
        )
    else:
        target = DriveStorageTarget(
            drive_id=drive_id, role=StorageTargetRole.SLAVE, status=initial_status,
            preset_id=preset.id,
        )

    db.add(target)
    record_event(
        db, user.sub, STORAGE_TARGET_ADDED, ctx, session_id=session_key(user, ctx),
        driveId=str(drive_id), title=drive.title,
    )
    await db.commit()
    target = await _get_target_or_404(db, drive_id, target.id)
    return _to_target_out(target)


@router.post("/{target_id}/test-connection", response_model=ConnectionTestOut)
async def test_storage_target(
    drive_id: uuid.UUID,
    target_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
) -> ConnectionTestOut:
    """Tests an already-saved target's stored credentials — the backend
    resolves and decrypts them itself, the caller never sends secrets
    back over the wire for this. Always 200: a failed connection is a
    result to show the user, not an API error."""
    await get_accessible_drive_or_404(db, drive_id, user.sub)
    target = await _get_target_or_404(db, drive_id, target_id)
    ok, error = await storage.test_connection(resolve_target(target))
    return ConnectionTestOut(ok=ok, error=error)


@test_router.post("/test", response_model=ConnectionTestOut)
async def test_custom_config(
    payload: CustomStorageConfigIn,
    user: CurrentUser = Depends(get_current_user),
    storage: BlockStorage = Depends(get_storage),
) -> ConnectionTestOut:
    """Tests connection params the user just typed in, before they've been
    saved as any drive's target — e.g. the Custom-mode form in
    create-drive-page.tsx / drive-settings-page.tsx's add-slave form."""
    target = ResolvedTarget(
        id=uuid.uuid4(),
        role=StorageTargetRole.SLAVE,  # unused by test_connection, just satisfying the dataclass
        endpoint_url=payload.endpoint_url,
        region=payload.region,
        bucket=payload.bucket,
        access_key=payload.access_key,
        secret_key=payload.secret_key,
        use_ssl=payload.use_ssl,
    )
    ok, error = await storage.test_connection(target)
    return ConnectionTestOut(ok=ok, error=error)


@router.patch("/{target_id}/make-master", status_code=status.HTTP_204_NO_CONTENT)
async def make_master(
    drive_id: uuid.UUID,
    target_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    """Reassigning master is just swapping which row has role=MASTER — every
    ACTIVE/PROVISIONING target already receives every write via fan-out
    (owload-docs/decisions/0011), so no data needs to move and no active upload
    session needs to be paused."""
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    target = await _get_target_or_404(db, drive_id, target_id)

    if target.role == StorageTargetRole.MASTER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target is already the master")
    if target.status != StorageTargetStatus.ACTIVE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Target must be ACTIVE to become master")
    tier = target.preset.tier if target.preset_id is not None else S3Tier.HOT
    if tier != S3Tier.HOT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A cold-tier target cannot become master")

    current_master = (
        await db.execute(
            select(DriveStorageTarget).where(
                DriveStorageTarget.drive_id == drive_id, DriveStorageTarget.role == StorageTargetRole.MASTER
            )
        )
    ).scalar_one()
    current_master.role = StorageTargetRole.SLAVE
    target.role = StorageTargetRole.MASTER
    record_event(
        db, user.sub, STORAGE_TARGET_MAIN_CHANGED, ctx, session_id=session_key(user, ctx),
        driveId=str(drive_id), title=drive.title,
    )
    await db.commit()


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_storage_target(
    drive_id: uuid.UUID,
    target_id: uuid.UUID,
    confirm: bool = Query(False, description="Must be true — this permanently deletes the target's data"),
    force: bool = Query(
        False,
        description="Remove the target even if its data couldn't be purged (e.g. its S3 is "
        "unreachable) — leaves orphaned data behind on that storage",
    ),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    target = await _get_target_or_404(db, drive_id, target_id)

    if target.role == StorageTargetRole.MASTER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Cannot remove the master target — reassign master first"
        )
    if not confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass confirm=true to permanently delete this target's data")

    # Flip to REMOVING (and commit) before the actual S3 purge: fan-out reads
    # this status on every subsequent write (get_active_targets), so writes
    # stop going to this target immediately, even though deleting its
    # (possibly large) backlog of objects can take a while.
    resolved = resolve_target(target)
    target.status = StorageTargetStatus.REMOVING
    await db.commit()

    try:
        await storage.purge_target_data(resolved, drive_id)
    except Exception as exc:
        # Broad on purpose: BlockDeleteError only covers a *reachable* S3
        # reporting per-key failures. A totally unreachable target (host
        # down, credentials revoked — this is the owner's own custom
        # infrastructure, which they don't necessarily control the uptime
        # of) raises something else entirely, and without `force` that
        # would leave this target stuck REMOVING forever with no way out.
        if not force:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Failed to delete the target's data ({exc}); it stays REMOVING and this can be "
                f"retried, or pass force=true to remove it anyway (data may be left behind on that storage)",
            ) from exc
        logger.error(
            "force-removing target %s despite failed data purge (drive=%s): %s",
            target.id, drive_id, exc, exc_info=True,
        )

    await db.delete(target)
    record_event(
        db, user.sub, STORAGE_TARGET_REMOVED, ctx, session_id=session_key(user, ctx),
        driveId=str(drive_id), title=drive.title,
    )
    await db.commit()
