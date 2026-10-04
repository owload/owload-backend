"""Compacts the per-drive ops/ recovery backup (see app/services/storage.py:
BlockStorage.append_ops_backup) into fewer, larger ops-batches/ objects.

A drive can have several storage targets (owload-docs/decisions/0011) and the
fan-out write puts its own copy of every ops/ chunk on each of them, so
compaction runs per target: each ACTIVE/PROVISIONING target of each drive is
compacted independently, against that target's own S3 connection. One
target's state (or its being unreachable) never affects the others.

Finding the targets needs Postgres (drive_storage_targets holds the connection
details), like the backfill worker. The disaster-recovery path does not:
reconstruct_ops_from_backup() works from S3 alone given an explicit target —
it is what a recovery would use after losing Postgres, when the caller has to
supply the connection of the target to restore from.

Safe to crash at any point: the new batch object is written *before* the
source chunks it merges are deleted, so a run that dies in between leaves the
same bytes covered twice (batch + not-yet-deleted chunks) rather than covered
zero times. The next run detects and deletes those already-covered leftovers
before doing any new batching.

A PROVISIONING target may be compacted while its backfill is still running:
chunks are only merged while they are contiguous from what is already covered,
so a target that is still missing the early history just has nothing to merge
yet, and a chunk the backfill re-copies after it was merged is simply cleaned
up as an already-covered leftover on the next run.
"""

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import DriveStorageTarget, StorageTargetStatus
from app.services.storage import OPS_OFFSET_WIDTH, BlockStorage
from app.services.storage_targets import ResolvedTarget, resolve_target

logger = logging.getLogger(__name__)

_CHUNK_KEY_RE = re.compile(r"^ops/(?P<drive_id>[^/]+)/(?P<from>\d+)-(?P<to>\d+)\.bin$")
_BATCH_KEY_RE = re.compile(r"^ops-batches/(?P<drive_id>[^/]+)/batch-(?P<from>\d+)-(?P<to>\d+)\.bin$")

# Below this many contiguous chunks, merging isn't worth the S3 round trips.
_MIN_CHUNKS_TO_BATCH = 2


@dataclass(frozen=True)
class _Range:
    key: str
    from_offset: int
    to_offset: int


class OpsCompactionService:
    def __init__(self, storage: BlockStorage | None = None) -> None:
        self._storage = storage or BlockStorage()

    async def _list_ranges(self, client, bucket: str, prefix: str, pattern: re.Pattern) -> list[_Range]:
        ranges = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                m = pattern.match(obj["Key"])
                if m:
                    ranges.append(_Range(obj["Key"], int(m["from"]), int(m["to"])))
        ranges.sort(key=lambda r: r.from_offset)
        return ranges

    async def _delete(self, client, bucket: str, keys: list[str]) -> None:
        if not keys:
            return
        await client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys]})

    async def compact_drive(self, target: ResolvedTarget, drive_id: str) -> dict:
        """One compaction pass for a single drive on a single storage target.
        Idempotent and safe to call repeatedly / concurrently with new ops
        being appended — it only ever merges a snapshot of what's already
        there when it starts."""
        bucket = target.bucket
        async with self._storage._client_ctx(target) as client:
            chunks = await self._list_ranges(client, bucket, f"ops/{drive_id}/", _CHUNK_KEY_RE)
            batches = await self._list_ranges(client, bucket, f"ops-batches/{drive_id}/", _BATCH_KEY_RE)
            covered_upto = max((b.to_offset for b in batches), default=0)

            # Cleanup left behind by a previous run that batched these chunks
            # but crashed/was killed before deleting the originals.
            leftovers = [c for c in chunks if c.to_offset <= covered_upto]
            await self._delete(client, bucket, [c.key for c in leftovers])

            pending = [c for c in chunks if c.from_offset >= covered_upto]
            run: list[_Range] = []
            expected_from = covered_upto
            for c in pending:
                if c.from_offset != expected_from:
                    break  # gap (or a chunk still being written) — stop, next run picks it up
                run.append(c)
                expected_from = c.to_offset

            if len(run) < _MIN_CHUNKS_TO_BATCH:
                return {"drive_id": drive_id, "batched_chunks": 0, "cleaned_up": len(leftovers)}

            body = bytearray()
            for c in run:
                obj = await client.get_object(Bucket=bucket, Key=c.key)
                async with obj["Body"] as stream:
                    body.extend(await stream.read())

            batch_key = (
                f"ops-batches/{drive_id}/batch-"
                f"{run[0].from_offset:0{OPS_OFFSET_WIDTH}d}-{run[-1].to_offset:0{OPS_OFFSET_WIDTH}d}.bin"
            )
            # Write the merged batch BEFORE deleting the originals — a crash
            # here just leaves temporary double coverage, never data loss.
            await client.put_object(Bucket=bucket, Key=batch_key, Body=bytes(body))
            await self._delete(client, bucket, [c.key for c in run])

            return {"drive_id": drive_id, "batched_chunks": len(run), "cleaned_up": len(leftovers)}

    async def compact_all_drives(self, db: AsyncSession) -> list[dict]:
        """Compacts every drive on every one of its ACTIVE/PROVISIONING
        storage targets (the same set the fan-out write goes to). Each
        (drive, target) pair is independent: a failure — typically an
        unreachable custom target — is logged and skipped, and retried on the
        next pass, without holding up the other targets."""
        rows = (
            await db.execute(
                select(DriveStorageTarget)
                .options(selectinload(DriveStorageTarget.preset))
                .where(
                    DriveStorageTarget.status.in_(
                        [StorageTargetStatus.ACTIVE, StorageTargetStatus.PROVISIONING]
                    )
                )
                .order_by(DriveStorageTarget.drive_id)
            )
        ).scalars().all()

        results = []
        for row in rows:
            try:
                result = await self.compact_drive(resolve_target(row), str(row.drive_id))
                results.append({**result, "target_id": str(row.id)})
            except Exception:
                logger.exception(
                    "compaction failed for drive=%s target=%s, will retry next pass", row.drive_id, row.id
                )
        return results


async def list_ops_ranges(client, bucket: str, drive_id: str) -> list[_Range]:
    """Every ops/ chunk and ops-batches/ batch of a drive on one storage, as
    ranges ordered by start offset (longest first on ties). The ranges may
    overlap — a crashed compaction leaves bytes covered twice — and may have
    gaps; the caller decides what to do about either."""
    service = OpsCompactionService()
    chunks = await service._list_ranges(client, bucket, f"ops/{drive_id}/", _CHUNK_KEY_RE)
    batches = await service._list_ranges(client, bucket, f"ops-batches/{drive_id}/", _BATCH_KEY_RE)
    return sorted(chunks + batches, key=lambda r: (r.from_offset, -r.to_offset))


async def reconstruct_ops_from_backup(target: ResolvedTarget, drive_id: str) -> bytes:
    """Rebuilds a drive's full ops log purely from the S3 recovery backup
    held by `target` (both ops/ and ops-batches/ prefixes), deduplicating any
    range that a crashed compaction run left covered twice.

    The caller chooses the target explicitly. Reads in this system always go
    to the MASTER (decisions/0011), so a normal recovery restores from the
    drive's master target (see storage_targets.get_master); after losing
    Postgres the caller has to build that ResolvedTarget by hand from the
    target's known connection details. Also used by the tests to verify that
    compaction never loses or corrupts data."""
    service = OpsCompactionService()
    bucket = target.bucket
    async with service._storage._client_ctx(target) as client:
        chunks = await service._list_ranges(client, bucket, f"ops/{drive_id}/", _CHUNK_KEY_RE)
        batches = await service._list_ranges(client, bucket, f"ops-batches/{drive_id}/", _BATCH_KEY_RE)
        all_ranges = sorted(chunks + batches, key=lambda r: (r.from_offset, -r.to_offset))

        result = bytearray()
        covered_upto = 0
        for r in all_ranges:
            if r.to_offset <= covered_upto:
                continue  # fully covered by a range already taken
            obj = await client.get_object(Bucket=bucket, Key=r.key)
            async with obj["Body"] as stream:
                content = await stream.read()
            start_in_range = max(0, covered_upto - r.from_offset)
            result.extend(content[start_in_range:])
            covered_upto = r.to_offset
        return bytes(result)


_compaction_service: OpsCompactionService | None = None


def get_compaction_service() -> OpsCompactionService:
    global _compaction_service
    if _compaction_service is None:
        _compaction_service = OpsCompactionService()
    return _compaction_service
