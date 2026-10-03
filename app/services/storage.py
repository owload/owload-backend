import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from datetime import datetime
from typing import NoReturn

import aioboto3
from botocore.exceptions import ClientError

from app.services.storage_errors import TargetFailure, classify_storage_error
from app.services.storage_targets import ResolvedTarget, get_master

logger = logging.getLogger(__name__)

BLOCK_SIZE = 1024 * 1024  # 1 MiB — unit of read-modify-write for partial block writes
CONNECTION_TEST_TIMEOUT_SECONDS = 5

# Zero-padded width for ops byte-range keys (ops/{driveId}/{from}-{to}.bin and,
# later, ops-batches/{driveId}/batch-{from}-{to}.bin). 20 digits comfortably
# covers the full bigint range so lexicographic S3 listing sorts correctly.
OPS_OFFSET_WIDTH = 20


class BlockDeleteError(RuntimeError):
    """One or more keys in a delete_objects call came back in the response's
    "Errors" list — a real per-key failure (permissions, transient S3 error),
    distinct from a key simply not existing (which S3 reports as success)."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} block(s) failed to delete: {errors}")


class BlockWriteError(RuntimeError):
    """A fan-out write (see owload-docs/decisions/0011) failed on one or more of
    a drive's storage targets. Whatever succeeded on the other targets has
    already been rolled back (compensating delete) on a best-effort basis —
    a rollback that itself fails is a known, accepted risk, logged but not
    re-raised (see owload-docs/epics/0014-storage-replication-reconciliation).

    `errors` holds the raw exceptions (server-side use only). `failures` is the
    same information classified and stripped of anything that must not reach a
    client (owload-docs/epics/0021); `rolled_back` is False if a compensating
    delete failed somewhere, i.e. part of the write may remain. The message does
    not include the raw exceptions, so str(exc) is safe to log but is not meant
    for the client either — build the response from `failures`."""

    def __init__(
        self,
        errors: dict[uuid.UUID, BaseException],
        targets: Sequence[ResolvedTarget] = (),
        rolled_back: bool = True,
    ) -> None:
        self.errors = errors
        self.rolled_back = rolled_back
        by_id = {t.id: t for t in targets}
        self.failures = [
            TargetFailure(
                target_id=target_id,
                role=by_id[target_id].role if target_id in by_id else "",
                label=(by_id[target_id].label if target_id in by_id else "") or "Storage",
                reason=classify_storage_error(exc),
            )
            for target_id, exc in errors.items()
        ]
        summary = ", ".join(f"{f.target_id} ({f.reason})" for f in self.failures)
        super().__init__(f"write failed on {len(errors)} target(s): {summary}")


class BlockStorage:
    """Backs each drive's byte-addressable data area with fixed-size
    blocks, one S3 object per block, fanned out across all of a drive's
    active storage targets (owload-docs/decisions/0011). S3 has no in-place
    partial write, so a write that doesn't fully cover a block does a
    read-modify-write of just that block (read from MASTER only — targets
    are kept byte-identical by this same fan-out, so reading from every
    target would be redundant, not safer); a write spanning many full
    blocks touches each once.

    Concurrent writes to the *same* block are not additionally locked beyond
    S3's own last-write-wins semantics — acceptable for how the frontend uses
    this today (each upload session writes its own dedicated byte range).
    """

    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client_ctx(self, target: ResolvedTarget):
        return self._session.client(
            "s3",
            endpoint_url=target.endpoint_url,
            region_name=target.region,
            aws_access_key_id=target.access_key,
            aws_secret_access_key=target.secret_key,
            use_ssl=target.use_ssl,
        )

    @staticmethod
    def _block_key(drive_id: uuid.UUID, block_index: int) -> str:
        return f"data/{drive_id}/{block_index:012d}"

    async def _get_block(self, client, bucket: str, key: str) -> bytes:
        """Fetches a whole object — used by the write path, which needs the
        full existing content to merge a partial write into before PUTting
        the block back (S3 has no in-place partial write)."""
        try:
            resp = await client.get_object(Bucket=bucket, Key=key)
            async with resp["Body"] as body:
                return await body.read()
        except client.exceptions.NoSuchKey:
            return b""

    async def _get_block_range(self, client, bucket: str, key: str, offset_in_block: int, length: int) -> bytes:
        """Fetches only [offset_in_block, offset_in_block + length) via an S3
        Range request — used by the read path so it doesn't have to download
        a whole 1 MiB block just to return a small slice of it."""
        try:
            resp = await client.get_object(
                Bucket=bucket, Key=key, Range=f"bytes={offset_in_block}-{offset_in_block + length - 1}"
            )
            async with resp["Body"] as body:
                return await body.read()
        except client.exceptions.NoSuchKey:
            return b""
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidRange":
                # offset_in_block is at or past the end of the (shorter-than-expected) object.
                return b""
            raise

    async def _rollback_puts(self, clients: dict[uuid.UUID, object], committed: list[tuple[ResolvedTarget, str]]) -> bool:
        """Best-effort compensating DELETE for keys that were successfully
        PUT on some targets before the fan-out failed on another. A failure
        here is logged, not raised — the caller is already raising the
        original error, and this is an accepted risk (see BlockWriteError).
        Returns True if every delete went through (or there was nothing to
        delete), False if part of the write may remain."""

        async def _delete_one(target: ResolvedTarget, key: str) -> bool:
            try:
                await clients[target.id].delete_object(Bucket=target.bucket, Key=key)
                return True
            except Exception:
                logger.error(
                    "compensating rollback delete failed: target=%s bucket=%s key=%s",
                    target.id, target.bucket, key, exc_info=True,
                )
                return False

        results = await asyncio.gather(*(_delete_one(t, k) for t, k in committed), return_exceptions=True)
        return all(r is True for r in results)

    async def _raise_write_error(
        self,
        clients: dict[uuid.UUID, object],
        committed: list[tuple[ResolvedTarget, str]],
        targets: list[ResolvedTarget],
        errors: dict[uuid.UUID, BaseException],
    ) -> NoReturn:
        """Common tail of a failed fan-out write: log what really happened (the
        only place the raw errors are kept), undo what already succeeded, and
        raise a BlockWriteError that carries the classified, client-safe view."""
        for target_id, exc in errors.items():
            logger.error("storage write failed: target=%s", target_id, exc_info=exc)
        rolled_back = await self._rollback_puts(clients, committed)
        raise BlockWriteError(errors, targets, rolled_back)

    async def write_range(self, targets: list[ResolvedTarget], drive_id: uuid.UUID, offset: int, data: bytes) -> None:
        if not data:
            return

        master = get_master(targets)
        committed: list[tuple[ResolvedTarget, str]] = []

        async with AsyncExitStack() as stack:
            clients = {t.id: await stack.enter_async_context(self._client_ctx(t)) for t in targets}
            master_client = clients[master.id]

            pos = 0
            while pos < len(data):
                abs_pos = offset + pos
                block_index = abs_pos // BLOCK_SIZE
                offset_in_block = abs_pos - block_index * BLOCK_SIZE
                take = min(BLOCK_SIZE - offset_in_block, len(data) - pos)
                chunk = data[pos : pos + take]
                key = self._block_key(drive_id, block_index)

                if offset_in_block == 0 and take == BLOCK_SIZE:
                    new_block = chunk
                else:
                    try:
                        existing = bytearray(await self._get_block(master_client, master.bucket, key))
                    except Exception as exc:
                        # The master could not even be read to merge the partial block.
                        await self._raise_write_error(clients, committed, targets, {master.id: exc})
                    end_in_block = offset_in_block + take
                    if len(existing) < end_in_block:
                        existing.extend(b"\x00" * (end_in_block - len(existing)))
                    existing[offset_in_block:end_in_block] = chunk
                    new_block = bytes(existing)

                results = await asyncio.gather(
                    *(clients[t.id].put_object(Bucket=t.bucket, Key=key, Body=new_block) for t in targets),
                    return_exceptions=True,
                )
                errors = {t.id: r for t, r in zip(targets, results) if isinstance(r, BaseException)}
                committed.extend((t, key) for t, r in zip(targets, results) if not isinstance(r, BaseException))

                if errors:
                    await self._raise_write_error(clients, committed, targets, errors)

                pos += take

    async def free_range(self, targets: list[ResolvedTarget], drive_id: uuid.UUID, start: int, end: int) -> int:
        """Frees [start, end) in a drive's data area — called when a
        client-side file spanning that range is deleted. The backend has no
        notion of "files" (only the client's ops log knows file boundaries),
        so the only thing it can safely do is: delete a block's S3 object
        outright if [start, end) fully covers that block (nothing outside
        the freed range can live there, so nothing is lost); leave any block
        the range only partially overlaps completely untouched, since bytes
        outside [start, end) in that block belong to something else still
        alive. Deletes on every active target; a per-key failure on any
        target fails the whole call (no compensation — there's nothing to
        restore a delete from). Returns the number of blocks targeted."""
        if end <= start:
            return 0

        first_block = start // BLOCK_SIZE
        last_block = (end - 1) // BLOCK_SIZE
        keys = []
        for block_index in range(first_block, last_block + 1):
            block_start = block_index * BLOCK_SIZE
            block_end = block_start + BLOCK_SIZE
            if start <= block_start and end >= block_end:
                keys.append(self._block_key(drive_id, block_index))

        if not keys:
            return 0

        errors: list[dict] = []
        async with AsyncExitStack() as stack:
            clients = [await stack.enter_async_context(self._client_ctx(t)) for t in targets]
            for target, client in zip(targets, clients):
                for i in range(0, len(keys), 1000):  # delete_objects caps at 1000 keys/call
                    batch = keys[i : i + 1000]
                    resp = await client.delete_objects(
                        Bucket=target.bucket, Delete={"Objects": [{"Key": k} for k in batch]}
                    )
                    errors.extend(resp.get("Errors") or [])

        if errors:
            raise BlockDeleteError(errors)

        return len(keys)

    async def list_allocated_ranges(self, target: ResolvedTarget, drive_id: uuid.UUID) -> list[tuple[int, int]]:
        """Lists which byte ranges are currently backed by real block objects
        in S3 for a drive, merged into contiguous runs. Reads MASTER only.
        Cheap: only lists object keys (list_objects_v2), never reads block
        content — cost scales with block *count* (paginated, 1000 keys/page),
        not with data size. Meant for a client to diff against what its ops
        log says should be allocated, then DELETE /data whatever's left over."""
        prefix = f"data/{drive_id}/"
        block_indices: list[int] = []
        async with self._client_ctx(target) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=target.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    block_indices.append(int(obj["Key"].rsplit("/", 1)[-1]))
        block_indices.sort()

        ranges: list[tuple[int, int]] = []
        for idx in block_indices:
            start = idx * BLOCK_SIZE
            end = start + BLOCK_SIZE
            if ranges and ranges[-1][1] == start:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        return ranges

    async def iter_range(self, target: ResolvedTarget, drive_id: uuid.UUID, start: int, length: int) -> AsyncIterator[bytes]:
        """Yields the requested range one block-chunk at a time, reading from
        MASTER only, so a caller streaming this to a client (e.g. a
        StreamingResponse) can stop pulling further blocks from S3 as soon as
        the client goes away, instead of always fetching the whole range up
        front."""
        if length <= 0:
            return
        async with self._client_ctx(target) as client:
            pos = 0
            while pos < length:
                abs_pos = start + pos
                block_index = abs_pos // BLOCK_SIZE
                offset_in_block = abs_pos - block_index * BLOCK_SIZE
                take = min(BLOCK_SIZE - offset_in_block, length - pos)
                key = self._block_key(drive_id, block_index)
                chunk = await self._get_block_range(client, target.bucket, key, offset_in_block, take)
                if len(chunk) < take:
                    chunk = chunk + b"\x00" * (take - len(chunk))
                yield chunk
                pos += take

    async def purge_target_data(self, target: ResolvedTarget, drive_id: uuid.UUID) -> None:
        """Deletes every object a drive has ever written to a single target
        (data blocks, ops backup chunks/batches, drive meta) — used when a
        slave target is removed (owload-docs/epics/0012 task 0005). Only ever
        touches the one target passed in; the caller is responsible for
        having already taken it out of fan-out (status=REMOVING) first."""
        prefixes = (
            f"data/{drive_id}/",
            f"ops/{drive_id}/",
            f"ops-batches/{drive_id}/",
            f"drives/{drive_id}/",
        )
        errors: list[dict] = []
        async with self._client_ctx(target) as client:
            paginator = client.get_paginator("list_objects_v2")
            for prefix in prefixes:
                async for page in paginator.paginate(Bucket=target.bucket, Prefix=prefix):
                    keys = [obj["Key"] for obj in page.get("Contents", [])]
                    for i in range(0, len(keys), 1000):  # delete_objects caps at 1000 keys/call
                        batch = keys[i : i + 1000]
                        resp = await client.delete_objects(
                            Bucket=target.bucket, Delete={"Objects": [{"Key": k} for k in batch]}
                        )
                        errors.extend(resp.get("Errors") or [])

        if errors:
            raise BlockDeleteError(errors)

    async def _fanout_put(
        self, targets: list[ResolvedTarget], key: str, body: bytes, content_type: str | None = None
    ) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        async with AsyncExitStack() as stack:
            clients = {t.id: await stack.enter_async_context(self._client_ctx(t)) for t in targets}
            results = await asyncio.gather(
                *(clients[t.id].put_object(Bucket=t.bucket, Key=key, Body=body, **extra) for t in targets),
                return_exceptions=True,
            )
            errors = {t.id: r for t, r in zip(targets, results) if isinstance(r, BaseException)}
            committed = [(t, key) for t, r in zip(targets, results) if not isinstance(r, BaseException)]

            if errors:
                await self._raise_write_error(clients, committed, targets, errors)

    async def test_connection(self, target: ResolvedTarget) -> tuple[bool, str | None]:
        """Checks that `target`'s credentials/endpoint can actually reach
        its bucket — backs the storage-target "Test connection" UI
        (owload-docs/epics/0012 task 0009). Never raises: any failure (bad
        credentials, unreachable host, missing bucket, timeout) becomes
        (False, <message>) so the route can always respond 200, since a
        failed *test* isn't itself an API error."""
        try:
            async with asyncio.timeout(CONNECTION_TEST_TIMEOUT_SECONDS):
                async with self._client_ctx(target) as client:
                    await client.head_bucket(Bucket=target.bucket)
            return True, None
        except TimeoutError:
            return False, f"Connection timed out after {CONNECTION_TEST_TIMEOUT_SECONDS}s"
        except Exception as exc:
            return False, str(exc)

    async def write_drive_meta(
        self,
        targets: list[ResolvedTarget],
        drive_id: uuid.UUID,
        owner: str,
        title: str,
        key_nonce: str,
        counter_nonce: str,
        created_at: datetime,
    ) -> None:
        """Writes drives/{driveId}/meta.json — a standalone recovery record
        with everything needed to reconstruct a drive's identity/nonces
        without Postgres. Written once at creation; nothing here changes
        afterwards, so there's nothing to keep in sync later. Fanned out to
        every active target."""
        meta = {
            "driveId": str(drive_id),
            "owner": owner,
            "title": title,
            "keyNonce": key_nonce,
            "counterNonce": counter_nonce,
            "createdAt": created_at.isoformat(),
        }
        await self._fanout_put(
            targets, f"drives/{drive_id}/meta.json", json.dumps(meta).encode("utf-8"), "application/json"
        )

    async def append_ops_backup(
        self, targets: list[ResolvedTarget], drive_id: uuid.UUID, chunk: bytes, from_offset: int, to_offset: int
    ) -> None:
        """Dual-writes one saveOperation() chunk to
        ops/{driveId}/{from}-{to}.bin, mirroring the byte range it occupies in
        the Postgres ops_data log. A separate, later batching job can list
        this prefix, concatenate ranges into ops-batches/{driveId}/batch-{from}-{to}.bin,
        and delete the originals — recovery just needs to list both prefixes,
        sort by `from`, and concatenate. Fanned out to every active target."""
        if not chunk:
            return
        key = f"ops/{drive_id}/{from_offset:0{OPS_OFFSET_WIDTH}d}-{to_offset:0{OPS_OFFSET_WIDTH}d}.bin"
        await self._fanout_put(targets, key, chunk)


_storage: BlockStorage | None = None


def get_storage() -> BlockStorage:
    global _storage
    if _storage is None:
        _storage = BlockStorage()
    return _storage
