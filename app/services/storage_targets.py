"""Resolves a drive's configured S3 storage targets (owload-docs/decisions/0011)
into plain connection configs BlockStorage can build S3 clients from —
decrypting secrets and flattening "preset or custom_config" into one shape.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.db.models import (
    DriveStorageTarget,
    S3Preset,
    StorageTargetRole,
    StorageTargetStatus,
)
from app.services.secrets import decrypt_secret

_LEGACY_GLOBAL_PRESET_LABEL = "legacy-global (implicit)"
CUSTOM_TARGET_LABEL = "Custom storage"


@dataclass(frozen=True)
class ResolvedTarget:
    id: uuid.UUID
    role: str
    endpoint_url: str | None
    region: str
    bucket: str
    access_key: str
    secret_key: str  # already decrypted
    use_ssl: bool
    # Human-readable name that is safe to show to the drive's users in error
    # messages: the preset's label, or a neutral name for a custom target. It
    # must never contain the endpoint, bucket or credentials.
    label: str = ""


def resolve_preset(preset: S3Preset) -> ResolvedTarget:
    """Resolves a bare preset (not yet attached to any drive) into a
    connectable target — e.g. for testing a preset's own reachability
    directly by presetId (owload-docs/epics/0012 task 0009). `role` is
    meaningless here (no drive to be MASTER/SLAVE *of*) and left blank."""
    return ResolvedTarget(
        id=preset.id,
        role="",
        endpoint_url=preset.endpoint_url,
        region=preset.region,
        bucket=preset.bucket,
        access_key=preset.access_key,
        secret_key=decrypt_secret(preset.secret_key),
        use_ssl=preset.use_ssl,
        label=preset.label,
    )


def resolve_target(row: DriveStorageTarget) -> ResolvedTarget:
    if row.preset_id is not None:
        preset = row.preset
        return ResolvedTarget(
            id=row.id,
            role=row.role,
            endpoint_url=preset.endpoint_url,
            region=preset.region,
            bucket=preset.bucket,
            access_key=preset.access_key,
            secret_key=decrypt_secret(preset.secret_key),
            use_ssl=preset.use_ssl,
            label=preset.label,
        )
    cfg = row.custom_config
    return ResolvedTarget(
        id=row.id,
        role=row.role,
        endpoint_url=cfg.get("endpoint_url"),
        region=cfg["region"],
        bucket=cfg["bucket"],
        access_key=cfg["access_key"],
        secret_key=decrypt_secret(cfg["secret_key"]),
        use_ssl=cfg.get("use_ssl", True),
        label=CUSTOM_TARGET_LABEL,
    )


async def get_active_targets(db: AsyncSession, drive_id: uuid.UUID) -> list[ResolvedTarget]:
    """Targets writes fan out to: ACTIVE or PROVISIONING (see decisions/0011
    — PROVISIONING targets are mid-backfill but already receiving new writes)."""
    result = await db.execute(
        select(DriveStorageTarget)
        .options(selectinload(DriveStorageTarget.preset))
        .where(
            DriveStorageTarget.drive_id == drive_id,
            DriveStorageTarget.status.in_([StorageTargetStatus.ACTIVE, StorageTargetStatus.PROVISIONING]),
        )
    )
    return [resolve_target(row) for row in result.scalars().all()]


def get_master(targets: list[ResolvedTarget]) -> ResolvedTarget:
    """Picks MASTER out of an already-resolved target list — reads only ever
    go to master (decisions/0011), and master is always ACTIVE (a precondition
    for becoming master at all), so it's always present among active targets."""
    for target in targets:
        if target.role == StorageTargetRole.MASTER:
            return target
    raise RuntimeError("no MASTER target found among active targets — data model invariant violated")


async def get_or_create_legacy_global_preset(db: AsyncSession, settings: Settings | None = None) -> S3Preset:
    """Returns the shared implicit preset mirroring the global S3_* settings
    (created by migration 0005 for pre-existing drives). Creates it on first
    use if it doesn't exist yet (e.g. a fresh DB with zero drives at
    migration time) — new drives need *some* target until task 0004 adds
    real preset/custom_config selection at creation time."""
    settings = settings or get_settings()
    existing = (
        await db.execute(select(S3Preset).where(S3Preset.label == _LEGACY_GLOBAL_PRESET_LABEL))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    from app.services.secrets import encrypt_secret

    preset = S3Preset(
        label=_LEGACY_GLOBAL_PRESET_LABEL,
        tier="HOT",
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=encrypt_secret(settings.s3_secret_key),
        use_ssl=settings.s3_use_ssl,
    )
    db.add(preset)
    await db.flush()
    return preset
