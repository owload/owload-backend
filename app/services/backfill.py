"""Copies a drive's existing S3 backlog (data blocks, ops backup, ops
batches, drive meta) from its master onto a slave target that's still
`PROVISIONING` (added via POST /drives/{driveId}/storage-targets, epic
0012 task 0005), then flips that target to `ACTIVE`.

Like app/services/compaction.py, this needs Postgres: to know which target
is PROVISIONING and to resolve both its and its drive's master's connection
details (drive_storage_targets).

Order matters (see owload-docs/decisions/0012): a target only ever becomes
PROVISIONING *after* it's already receiving live fan-out writes (epic
0012 task 0002), so this only has to copy whatever accumulated *before*
that point — anything newer already lands on it directly. A block
written concurrently with this worker's own copy of the same block is a
known, accepted race (last-write-wins) — see
owload-docs/epics/0014-storage-replication-reconciliation.

Idempotent by construction: copying just overwrites the same keys with
the same bytes, so a crash or a re-run at any point is harmless — there's
no separate "how far did we get" state to track or corrupt.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import DriveStorageTarget, StorageTargetStatus
from app.services.storage import BlockStorage
from app.services.storage_targets import ResolvedTarget, get_active_targets, get_master, resolve_target

logger = logging.getLogger(__name__)

# Every prefix a drive's data can live under across its storage targets
# (mirrors BlockStorage.purge_target_data's own prefix list) — "drives"
# covers drives/{driveId}/meta.json, which write_drive_meta only ever
# fans out to the targets that already existed *at drive creation*, so a
# slave added later needs it backfilled just like everything else.
BACKFILL_PREFIXES = ("data", "ops", "ops-batches", "drives")


class BackfillService:
    def __init__(self, storage: BlockStorage | None = None) -> None:
        self._storage = storage or BlockStorage()

    @staticmethod
    async def _count_objects(client, bucket: str, prefix: str) -> int:
        count = 0
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            count += len(page.get("Contents", []))
        return count

    async def _copy_prefix(self, master: ResolvedTarget, target: ResolvedTarget, prefix: str) -> tuple[int, int]:
        """Copies every object master currently has under `prefix` onto
        target. Returns (copied, total)."""
        total = 0
        copied = 0
        async with (
            self._storage._client_ctx(master) as master_client,
            self._storage._client_ctx(target) as target_client,
        ):
            paginator = master_client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=master.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    total += 1
                    key = obj["Key"]
                    resp = await master_client.get_object(Bucket=master.bucket, Key=key)
                    async with resp["Body"] as body:
                        data = await body.read()
                    await target_client.put_object(Bucket=target.bucket, Key=key, Body=data)
                    copied += 1
        return copied, total

    async def backfill_target(self, db: AsyncSession, target_row: DriveStorageTarget) -> dict:
        """One backfill pass for a single PROVISIONING target: copy
        master's current backlog onto it, then mark it ACTIVE."""
        drive_id = target_row.drive_id
        master = get_master(await get_active_targets(db, drive_id))
        target = resolve_target(target_row)

        stats = {}
        for prefix_name in BACKFILL_PREFIXES:
            copied, total = await self._copy_prefix(master, target, f"{prefix_name}/{drive_id}/")
            stats[prefix_name] = {"copied": copied, "total": total}

        target_row.status = StorageTargetStatus.ACTIVE
        await db.commit()

        result = {"drive_id": str(drive_id), "target_id": str(target_row.id), **stats}
        logger.info("backfill complete: %s", result)
        return result

    async def backfill_all_pending(self, db: AsyncSession) -> list[dict]:
        """Discovers every PROVISIONING target across all drives and
        backfills each. A single target's failure is logged and skipped
        (picked up again next pass), not fatal to the others."""
        rows = (
            await db.execute(
                select(DriveStorageTarget)
                .options(selectinload(DriveStorageTarget.preset))
                .where(DriveStorageTarget.status == StorageTargetStatus.PROVISIONING)
            )
        ).scalars().all()

        results = []
        for row in rows:
            try:
                results.append(await self.backfill_target(db, row))
            except Exception:
                logger.exception(
                    "backfill failed for drive=%s target=%s, will retry next pass", row.drive_id, row.id
                )
        return results

    async def progress(self, db: AsyncSession, target_row: DriveStorageTarget) -> dict:
        """Read-only snapshot of how much of master's current backlog a
        still-PROVISIONING target already has, computed live from S3
        object counts (nothing persisted) — good enough for a Settings
        page progress indicator, not an exact byte count."""
        master = get_master(await get_active_targets(db, target_row.drive_id))
        target = resolve_target(target_row)

        copied_total = 0
        grand_total = 0
        for prefix_name in BACKFILL_PREFIXES:
            prefix = f"{prefix_name}/{target_row.drive_id}/"
            async with self._storage._client_ctx(master) as master_client:
                master_count = await self._count_objects(master_client, master.bucket, prefix)
            async with self._storage._client_ctx(target) as target_client:
                target_count = await self._count_objects(target_client, target.bucket, prefix)
            grand_total += master_count
            copied_total += min(target_count, master_count)
        return {"copied": copied_total, "total": grand_total}


_backfill_service: BackfillService | None = None


def get_backfill_service() -> BackfillService:
    global _backfill_service
    if _backfill_service is None:
        _backfill_service = BackfillService()
    return _backfill_service
