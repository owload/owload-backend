"""Tests the ops/ -> ops-batches/ compaction of a single storage target against
real S3 (whatever S3_* is configured — MinIO or a real bucket). Touches only S3,
no Postgres/API involved. Skips automatically if S3 isn't reachable. The
multi-target behaviour (every target of a drive, via Postgres) is covered in
test_integration.py.
"""

import uuid

import pytest

from app.core.config import get_settings
from app.services.compaction import OpsCompactionService, reconstruct_ops_from_backup
from app.services.storage import BlockStorage
from app.services.storage_targets import ResolvedTarget

pytestmark = pytest.mark.asyncio


def _target_from_settings() -> ResolvedTarget:
    settings = get_settings()
    return ResolvedTarget(
        id=uuid.uuid4(),
        role="MASTER",
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        use_ssl=settings.s3_use_ssl,
    )


@pytest.fixture(autouse=True)
async def _require_s3():
    settings = get_settings()
    storage = BlockStorage()
    try:
        async with storage._client_ctx(_target_from_settings()) as client:
            await client.list_objects_v2(Bucket=settings.s3_bucket, MaxKeys=1)
    except Exception as exc:
        pytest.skip(f"S3 not reachable, skipping compaction tests: {exc}")


async def _cleanup(drive_id: str) -> None:
    settings = get_settings()
    storage = BlockStorage()
    async with storage._client_ctx(_target_from_settings()) as client:
        for prefix in (f"ops/{drive_id}/", f"ops-batches/{drive_id}/", f"drives/{drive_id}/"):
            resp = await client.list_objects_v2(Bucket=settings.s3_bucket, Prefix=prefix)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            if keys:
                await client.delete_objects(
                    Bucket=settings.s3_bucket, Delete={"Objects": [{"Key": k} for k in keys]}
                )


async def test_compact_merges_contiguous_chunks_without_data_loss():
    storage = BlockStorage()
    target = _target_from_settings()
    drive_id = str(uuid.uuid4())

    pieces = [b"Hello ", b"world", b"!", b" More data after that."]
    offset = 0
    for piece in pieces:
        await storage.append_ops_backup([target], drive_id, piece, offset, offset + len(piece))
        offset += len(piece)
    expected = b"".join(pieces)

    try:
        assert await reconstruct_ops_from_backup(target, drive_id) == expected

        service = OpsCompactionService()
        stats = await service.compact_drive(target, drive_id)
        assert stats["batched_chunks"] == len(pieces)
        assert stats["cleaned_up"] == 0

        assert await reconstruct_ops_from_backup(target, drive_id) == expected

        async with storage._client_ctx(target) as client:
            remaining = await client.list_objects_v2(Bucket=target.bucket, Prefix=f"ops/{drive_id}/")
            assert remaining.get("KeyCount", 0) == 0
            batches = await client.list_objects_v2(Bucket=target.bucket, Prefix=f"ops-batches/{drive_id}/")
            assert batches.get("KeyCount") == 1
    finally:
        await _cleanup(drive_id)


async def test_compact_leaves_gap_for_a_later_run():
    storage = BlockStorage()
    target = _target_from_settings()
    drive_id = str(uuid.uuid4())

    # A gap: [0,5) and [5,10) are contiguous, but [20,25) is not reachable yet.
    await storage.append_ops_backup([target], drive_id, b"aaaaa", 0, 5)
    await storage.append_ops_backup([target], drive_id, b"bbbbb", 5, 10)
    await storage.append_ops_backup([target], drive_id, b"ccccc", 20, 25)

    try:
        service = OpsCompactionService()
        stats = await service.compact_drive(target, drive_id)
        assert stats["batched_chunks"] == 2  # only the contiguous [0,10) run

        async with storage._client_ctx(target) as client:
            remaining = await client.list_objects_v2(Bucket=target.bucket, Prefix=f"ops/{drive_id}/")
            remaining_keys = [o["Key"] for o in remaining.get("Contents", [])]
            assert len(remaining_keys) == 1
            assert remaining_keys[0].endswith("00000000000000000020-00000000000000000025.bin")
    finally:
        await _cleanup(drive_id)


async def test_compact_is_crash_safe_and_cleans_up_leftover_duplicates():
    """Simulates a run that wrote the batch but was killed before deleting
    the originals: the leftover chunks must be cleaned up (not re-batched,
    not double-counted) on the next run."""
    storage = BlockStorage()
    target = _target_from_settings()
    drive_id = str(uuid.uuid4())

    await storage.append_ops_backup([target], drive_id, b"12345", 0, 5)
    await storage.append_ops_backup([target], drive_id, b"67890", 5, 10)

    try:
        service = OpsCompactionService()
        first = await service.compact_drive(target, drive_id)
        assert first["batched_chunks"] == 2

        # Simulate the crash: re-create the already-batched chunk objects.
        await storage.append_ops_backup([target], drive_id, b"12345", 0, 5)
        await storage.append_ops_backup([target], drive_id, b"67890", 5, 10)

        second = await service.compact_drive(target, drive_id)
        assert second["batched_chunks"] == 0
        assert second["cleaned_up"] == 2

        assert await reconstruct_ops_from_backup(target, drive_id) == b"1234567890"

        async with storage._client_ctx(target) as client:
            remaining = await client.list_objects_v2(Bucket=target.bucket, Prefix=f"ops/{drive_id}/")
            assert remaining.get("KeyCount", 0) == 0
    finally:
        await _cleanup(drive_id)
