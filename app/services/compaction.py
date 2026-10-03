"""Compacts the per-drive ops/ recovery backup (see app/services/storage.py:
BlockStorage.append_ops_backup) into fewer, larger ops-batches/ objects.

Runs entirely against S3 — it never touches Postgres. This mirrors how a real
disaster-recovery reconstruction would work if Postgres were unavailable: the
ops log lives on as objects under two prefixes, and reconstruction is just
"list both, sort by `from`, concatenate, skipping anything already covered".

Safe to crash at any point: the new batch object is written *before* the
source chunks it merges are deleted, so a run that dies in between leaves the
same bytes covered twice (batch + not-yet-deleted chunks) rather than covered
zero times. The next run detects and deletes those already-covered leftovers
before doing any new batching.
"""

import re
from dataclasses import dataclass

import aioboto3

from app.core.config import Settings, get_settings
from app.services.storage import OPS_OFFSET_WIDTH

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
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = aioboto3.Session()

    def _client_ctx(self):
        return self._session.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            region_name=self._settings.s3_region,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            use_ssl=self._settings.s3_use_ssl,
        )

    async def _list_ranges(self, client, prefix: str, pattern: re.Pattern) -> list[_Range]:
        ranges = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._settings.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                m = pattern.match(obj["Key"])
                if m:
                    ranges.append(_Range(obj["Key"], int(m["from"]), int(m["to"])))
        ranges.sort(key=lambda r: r.from_offset)
        return ranges

    async def _delete(self, client, keys: list[str]) -> None:
        if not keys:
            return
        await client.delete_objects(
            Bucket=self._settings.s3_bucket, Delete={"Objects": [{"Key": k} for k in keys]}
        )

    async def compact_drive(self, drive_id: str) -> dict:
        """One compaction pass for a single drive. Idempotent and safe to
        call repeatedly / concurrently with new ops being appended — it only
        ever merges a snapshot of what's already there when it starts."""
        async with self._client_ctx() as client:
            chunks = await self._list_ranges(client, f"ops/{drive_id}/", _CHUNK_KEY_RE)
            batches = await self._list_ranges(client, f"ops-batches/{drive_id}/", _BATCH_KEY_RE)
            covered_upto = max((b.to_offset for b in batches), default=0)

            # Cleanup left behind by a previous run that batched these chunks
            # but crashed/was killed before deleting the originals.
            leftovers = [c for c in chunks if c.to_offset <= covered_upto]
            await self._delete(client, [c.key for c in leftovers])

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
                obj = await client.get_object(Bucket=self._settings.s3_bucket, Key=c.key)
                async with obj["Body"] as stream:
                    body.extend(await stream.read())

            batch_key = (
                f"ops-batches/{drive_id}/batch-"
                f"{run[0].from_offset:0{OPS_OFFSET_WIDTH}d}-{run[-1].to_offset:0{OPS_OFFSET_WIDTH}d}.bin"
            )
            # Write the merged batch BEFORE deleting the originals — a crash
            # here just leaves temporary double coverage, never data loss.
            await client.put_object(Bucket=self._settings.s3_bucket, Key=batch_key, Body=bytes(body))
            await self._delete(client, [c.key for c in run])

            return {"drive_id": drive_id, "batched_chunks": len(run), "cleaned_up": len(leftovers)}

    async def compact_all_drives(self) -> list[dict]:
        """Discovers drives with pending ops/ chunks and compacts each."""
        drive_ids: set[str] = set()
        async with self._client_ctx() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._settings.s3_bucket, Prefix="ops/", Delimiter="/"):
                for common_prefix in page.get("CommonPrefixes", []):
                    # "ops/<driveId>/" -> "<driveId>"
                    drive_ids.add(common_prefix["Prefix"].split("/")[1])

        return [await self.compact_drive(drive_id) for drive_id in sorted(drive_ids)]


async def reconstruct_ops_from_backup(settings: Settings, drive_id: str) -> bytes:
    """Rebuilds the full ops log for a drive purely from its S3 recovery
    backup (both ops/ and ops-batches/ prefixes), deduplicating any range
    that a crashed compaction run left covered twice. Used to verify
    compaction never loses or corrupts data; also the shape a real recovery
    tool would take if Postgres were gone."""
    service = OpsCompactionService(settings)
    async with service._client_ctx() as client:
        chunks = await service._list_ranges(client, f"ops/{drive_id}/", _CHUNK_KEY_RE)
        batches = await service._list_ranges(client, f"ops-batches/{drive_id}/", _BATCH_KEY_RE)
        all_ranges = sorted(chunks + batches, key=lambda r: (r.from_offset, -r.to_offset))

        result = bytearray()
        covered_upto = 0
        for r in all_ranges:
            if r.to_offset <= covered_upto:
                continue  # fully covered by a range already taken
            obj = await client.get_object(Bucket=settings.s3_bucket, Key=r.key)
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
        _compaction_service = OpsCompactionService(get_settings())
    return _compaction_service
