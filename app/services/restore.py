"""Reads the recovery copy of drives out of an S3 bucket the caller connects, to
bring them back into the system (owload-docs/decisions/0017,
owload-docs/epics/0022).

Everything here works from S3 alone: `drives/{id}/meta.json` gives the drive's
identity and nonces, `ops/` + `ops-batches/` give the operation log and
`data/` shows how far the data area extends. Nothing is written to S3.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from app.services.compaction import _Range, list_ops_ranges
from app.services.storage import BLOCK_SIZE, BlockStorage
from app.services.storage_targets import ResolvedTarget

logger = logging.getLogger(__name__)

MAX_SCANNED_DRIVES = 100
MAX_RESTORE_DRIVES = 20
MAX_META_BYTES = 16 * 1024
MAX_TITLE_CHARS = 255
MAX_NONCE_CHARS = 256
# The log is held in a single Postgres bytea; a bucket claiming more than this
# is refused rather than loaded into memory.
MAX_OPS_BYTES = 128 * 1024 * 1024

# Highest block index the 12-digit key format can hold.
_MAX_BLOCK_INDEX = 10**12 - 1


class RestoreStatus:
    RESTORABLE = "RESTORABLE"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    DAMAGED = "DAMAGED"
    RESTORED = "RESTORED"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


class DamageReason:
    META_MISSING = "META_MISSING"
    META_INVALID = "META_INVALID"
    OPS_GAP = "OPS_GAP"
    OPS_CORRUPT = "OPS_CORRUPT"
    OPS_TOO_LARGE = "OPS_TOO_LARGE"


@dataclass(frozen=True)
class DriveMeta:
    drive_id: uuid.UUID
    title: str
    key_nonce: str
    counter_nonce: str
    created_at: datetime


@dataclass(frozen=True)
class FoundDrive:
    drive_id: uuid.UUID
    meta: DriveMeta | None
    ops_ranges: list[_Range]  # the ranges that make the log, in order
    ops_bytes: int
    damage: str | None


def _is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")


def _valid_nonce(value: object) -> bool:
    # The server never decodes a nonce (the client does), so only its shape is checked.
    return isinstance(value, str) and 0 < len(value) <= MAX_NONCE_CHARS


def parse_meta(raw: bytes, expected_id: uuid.UUID) -> DriveMeta | None:
    """Validates a meta.json. The `owner` field is deliberately not read: it names
    an identity of the installation the drive came from (decisions/0017)."""
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or uuid.UUID(str(doc.get("driveId"))) != expected_id:
            return None
        title = doc.get("title")
        if not isinstance(title, str) or not title or len(title) > MAX_TITLE_CHARS:
            return None
        if not _valid_nonce(doc.get("keyNonce")) or not _valid_nonce(doc.get("counterNonce")):
            return None
        created_at = datetime.now(timezone.utc)
        raw_created = doc.get("createdAt")
        if isinstance(raw_created, str):
            try:
                parsed = datetime.fromisoformat(raw_created)
                created_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return DriveMeta(expected_id, title, doc["keyNonce"], doc["counterNonce"], created_at)
    except (ValueError, TypeError):
        return None


def plan_ops_log(ranges: list[_Range]) -> tuple[list[_Range], int] | None:
    """Picks, from ranges ordered by (start, longest first), the ones that make up
    the log, and the log's length. Bytes covered twice (a crashed compaction)
    are taken once. Returns None when the log has a hole: unlike a recovery that
    salvages what it can, a restore must not hand out a log with a missing
    stretch, because every later operation would be replayed on top of it."""
    chosen: list[_Range] = []
    covered = 0
    for r in ranges:
        if r.to_offset <= covered:
            continue
        if r.from_offset > covered:
            return None
        chosen.append(r)
        covered = r.to_offset
    return chosen, covered


def _meta_key(drive_id: uuid.UUID) -> str:
    return f"drives/{drive_id}/meta.json"


async def _read_meta(client, bucket: str, drive_id: uuid.UUID) -> tuple[DriveMeta | None, str | None]:
    try:
        # One byte past the cap is enough to tell that the file is too big.
        obj = await client.get_object(Bucket=bucket, Key=_meta_key(drive_id), Range=f"bytes=0-{MAX_META_BYTES}")
    except ClientError as exc:
        if _is_missing(exc):
            return None, DamageReason.META_MISSING
        raise
    async with obj["Body"] as stream:
        raw = await stream.read()
    if len(raw) > MAX_META_BYTES:
        return None, DamageReason.META_INVALID
    meta = parse_meta(raw, drive_id)
    return (meta, None) if meta else (None, DamageReason.META_INVALID)


async def _list_drive_ids(client, bucket: str) -> tuple[list[uuid.UUID], bool]:
    ids: list[uuid.UUID] = []
    paginator = client.get_paginator("list_objects_v2")
    async for page in paginator.paginate(Bucket=bucket, Prefix="drives/", Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            name = common["Prefix"][len("drives/"):].rstrip("/")
            try:
                drive_id = uuid.UUID(name)
            except ValueError:
                continue
            if str(drive_id) != name:
                continue  # only the canonical spelling the system itself writes
            if len(ids) == MAX_SCANNED_DRIVES:
                return ids, True
            ids.append(drive_id)
    return sorted(ids, key=str), False


async def inspect_drive(client, bucket: str, drive_id: uuid.UUID) -> FoundDrive:
    """Reads a drive's meta.json and checks that its operation log can be rebuilt.
    A damaged drive is reported with a reason; storage errors propagate."""
    meta, damage = await _read_meta(client, bucket, drive_id)
    if meta is None:
        return FoundDrive(drive_id, None, [], 0, damage)

    plan = plan_ops_log(await list_ops_ranges(client, bucket, str(drive_id)))
    if plan is None:
        return FoundDrive(drive_id, meta, [], 0, DamageReason.OPS_GAP)
    chosen, length = plan
    if length > MAX_OPS_BYTES:
        return FoundDrive(drive_id, meta, [], length, DamageReason.OPS_TOO_LARGE)
    return FoundDrive(drive_id, meta, chosen, length, None)


async def scan_bucket(storage: BlockStorage, target: ResolvedTarget) -> tuple[list[FoundDrive], bool]:
    async with storage._client_ctx(target) as client:
        ids, truncated = await _list_drive_ids(client, target.bucket)
        return [await inspect_drive(client, target.bucket, i) for i in ids], truncated


async def read_ops_log(client, bucket: str, found: FoundDrive) -> bytes | None:
    """Downloads the chosen ranges of the log. None if an object is missing or its
    size differs from the range in its name (a corrupt backup)."""
    result = bytearray()
    covered = 0
    for r in found.ops_ranges:
        try:
            obj = await client.get_object(Bucket=bucket, Key=r.key)
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise
        async with obj["Body"] as stream:
            content = await stream.read()
        if len(content) != r.to_offset - r.from_offset:
            return None
        result.extend(content[covered - r.from_offset:])
        covered = r.to_offset
    return bytes(result)


async def _has_key_after(client, bucket: str, drive_id: uuid.UUID, block_index: int | None) -> bool:
    """Whether the drive has a data block after `block_index` (None: any block)."""
    params = {"Bucket": bucket, "Prefix": f"data/{drive_id}/", "MaxKeys": 1}
    if block_index is not None:
        params["StartAfter"] = BlockStorage._block_key(drive_id, block_index)
    resp = await client.list_objects_v2(**params)
    return bool(resp.get("Contents"))


async def measure_data_length(client, bucket: str, drive_id: uuid.UUID) -> int:
    """The end of the data area as far as S3 shows it: the end of the last block
    that exists. Found by bisection over the block index (S3 lists keys in order
    and indexes are zero-padded), so the cost is about 40 requests however big
    the drive is, instead of listing every block."""
    if not await _has_key_after(client, bucket, drive_id, None):
        return 0
    # Smallest index with no key after it is the last block.
    lo, hi = 0, _MAX_BLOCK_INDEX
    while lo < hi:
        mid = (lo + hi) // 2
        if await _has_key_after(client, bucket, drive_id, mid):
            lo = mid + 1
        else:
            hi = mid
    head = await client.head_object(Bucket=bucket, Key=BlockStorage._block_key(drive_id, lo))
    return lo * BLOCK_SIZE + int(head["ContentLength"])
