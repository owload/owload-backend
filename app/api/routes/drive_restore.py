"""Restoring drives from an S3 bucket the user connects
(owload-docs/decisions/0017, owload-docs/epics/0022). A rescue path, separate
from creating a drive."""

import logging
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import storage_connection_failed
from app.core.security import CurrentUser, get_current_user
from app.db.models import (
    Drive,
    DriveAction,
    DriveStorageTarget,
    StorageTargetRole,
    StorageTargetStatus,
)
from app.db.session import get_db
from app.schemas.drive import CustomStorageConfigIn
from app.schemas.drive_restore import (
    FoundDriveOut,
    RestoreIn,
    RestoreOut,
    RestoreResultOut,
    RestoreScanIn,
    RestoreScanOut,
)
from app.services.activity import (
    DRIVE_RESTORED,
    ClientContext,
    get_client_context,
    record_event,
    session_key,
)
from app.services.audit import log_action
from app.services.restore import (
    DamageReason,
    FoundDrive,
    RestoreStatus,
    inspect_drive,
    measure_data_length,
    read_ops_log,
    scan_bucket,
)
from app.services.secrets import encrypt_secret
from app.services.storage import BlockStorage, get_storage
from app.services.storage_errors import classify_storage_error
from app.services.storage_targets import ResolvedTarget

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drives/restore", tags=["drive-restore"])


def _connection(cfg: CustomStorageConfigIn) -> ResolvedTarget:
    # A throwaway handle for talking to the bucket; nothing is stored from it
    # unless a drive is actually restored.
    return ResolvedTarget(
        id=uuid.uuid4(),
        role=StorageTargetRole.MASTER,
        endpoint_url=cfg.endpoint_url,
        region=cfg.region,
        bucket=cfg.bucket,
        access_key=cfg.access_key,
        secret_key=cfg.secret_key,
        use_ssl=cfg.use_ssl,
    )


def _fail(exc: Exception, what: str):
    """Logs the class of the error and its classification only: the message of a
    storage error carries the endpoint and bucket the caller typed in."""
    reason = classify_storage_error(exc)
    logger.warning("restore: %s failed (%s, %s)", what, type(exc).__name__, reason)
    return reason


async def _existing_ids(db: AsyncSession, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Ids already taken in the system, by any owner. Only existence is used."""
    if not ids:
        return set()
    result = await db.execute(select(Drive.id).where(Drive.id.in_(ids)))
    return set(result.scalars().all())


@router.post("/scan", response_model=RestoreScanOut)
async def scan(
    payload: RestoreScanIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
) -> RestoreScanOut:
    try:
        found, truncated = await scan_bucket(storage, _connection(payload.custom_config))
    except Exception as exc:
        raise storage_connection_failed(_fail(exc, "scan")) from exc

    existing = await _existing_ids(db, [f.drive_id for f in found])
    drives = []
    for f in found:
        if f.drive_id in existing:
            # Deliberately says nothing about the owner or the other storage.
            status_, reason = RestoreStatus.ALREADY_EXISTS, None
        elif f.damage:
            status_, reason = RestoreStatus.DAMAGED, f.damage
        else:
            status_, reason = RestoreStatus.RESTORABLE, None
        drives.append(
            FoundDriveOut(
                driveId=str(f.drive_id),
                title=f.meta.title if f.meta else None,
                createdTimestamp=int(f.meta.created_at.timestamp() * 1000) if f.meta else None,
                opsBytes=f.ops_bytes if f.meta else None,
                status=status_,
                reason=reason,
            )
        )
    return RestoreScanOut(drives=drives, truncated=truncated)


async def _restore_one(
    db: AsyncSession,
    user: CurrentUser,
    cfg: CustomStorageConfigIn,
    found: FoundDrive,
    ops_data: bytes,
    data_length: int,
    ctx: ClientContext,
) -> None:
    meta = found.meta
    assert meta is not None
    db.add(
        Drive(
            id=meta.drive_id,
            owner_user_id=user.sub,  # not meta.owner, see decisions/0017
            title=meta.title,
            key_nonce=meta.key_nonce,
            counter_nonce=meta.counter_nonce,
            acl={},
            ops_data=ops_data,
            data_length=data_length,
            created_at=meta.created_at,
        )
    )
    await db.flush()
    db.add(
        DriveStorageTarget(
            drive_id=meta.drive_id,
            role=StorageTargetRole.MASTER,
            status=StorageTargetStatus.ACTIVE,
            custom_config={
                "endpoint_url": cfg.endpoint_url,
                "region": cfg.region,
                "bucket": cfg.bucket,
                "access_key": cfg.access_key,
                "secret_key": encrypt_secret(cfg.secret_key),
                "use_ssl": cfg.use_ssl,
            },
        )
    )
    log_action(db, meta.drive_id, user.sub, DriveAction.RESTORE_DRIVE, title=meta.title)
    record_event(db, user.sub, DRIVE_RESTORED, ctx, session_id=session_key(user, ctx), driveId=str(meta.drive_id), title=meta.title)
    await db.commit()


@router.post("", response_model=RestoreOut)
async def restore(
    payload: RestoreIn,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> RestoreOut:
    cfg = payload.custom_config
    target = _connection(cfg)
    ids = list(dict.fromkeys(payload.drive_ids))
    existing = await _existing_ids(db, ids)
    results: list[RestoreResultOut] = []

    def add(drive_id: uuid.UUID, status_: str, reason: str | None = None) -> None:
        results.append(RestoreResultOut(driveId=str(drive_id), status=status_, reason=reason))

    async with storage._client_ctx(target) as client:
        for drive_id in ids:
            if drive_id in existing:
                add(drive_id, RestoreStatus.ALREADY_EXISTS)
                continue
            try:
                # Everything is re-read here instead of trusting a previous scan.
                found = await inspect_drive(client, target.bucket, drive_id)
                if found.meta is None and found.damage == DamageReason.META_MISSING:
                    add(drive_id, RestoreStatus.NOT_FOUND)
                    continue
                if found.damage:
                    add(drive_id, RestoreStatus.DAMAGED, found.damage)
                    continue
                ops_data = await read_ops_log(client, target.bucket, found)
                if ops_data is None:
                    add(drive_id, RestoreStatus.DAMAGED, DamageReason.OPS_CORRUPT)
                    continue
                data_length = await measure_data_length(client, target.bucket, drive_id)
                await _restore_one(db, user, cfg, found, ops_data, data_length, ctx)
                add(drive_id, RestoreStatus.RESTORED)
            except IntegrityError:
                # Someone restored or created the same id since the check above.
                await db.rollback()
                add(drive_id, RestoreStatus.ALREADY_EXISTS)
            except Exception as exc:
                await db.rollback()
                add(drive_id, RestoreStatus.FAILED, _fail(exc, f"drive {drive_id}"))
    return RestoreOut(results=results)
