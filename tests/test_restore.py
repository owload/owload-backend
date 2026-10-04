"""Restoring drives from an S3 bucket (owload-docs/epics/0022, decisions/0017).

The parsing and log-planning tests are pure. The endpoint tests need Postgres and
the local MinIO from docker-compose and skip when they are not reachable."""

import json
import uuid

import aioboto3
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive
from app.db.session import async_session_factory, engine
from app.main import app
from app.services.compaction import _Range
from app.services.restore import parse_meta, plan_ops_log
from app.services.storage import BLOCK_SIZE

MINIO_ENDPOINT = "http://localhost:9000"
MINIO_KEY = "minioadmin"
MINIO_SECRET = "minioadmin"

USER_A = CurrentUser(sub="restore-user-a", username="alice-test", email="alice-restore@example.com")
USER_B = CurrentUser(sub="restore-user-b", username="bob-test", email="bob-restore@example.com")

KEY_NONCE = "a2V5LW5vbmNlLTEyMzQ1Ng=="
COUNTER_NONCE = "Y291bnRlci1ub25jZS0x"


def _meta(drive_id: uuid.UUID, **overrides) -> bytes:
    doc = {
        "driveId": str(drive_id),
        "owner": "someone-from-another-installation",
        "title": "Photos",
        "keyNonce": KEY_NONCE,
        "counterNonce": COUNTER_NONCE,
        "createdAt": "2026-01-02T03:04:05+00:00",
    }
    doc.update(overrides)
    return json.dumps(doc).encode()


def _r(a: int, b: int) -> _Range:
    return _Range(f"k{a}-{b}", a, b)


# ---- pure ------------------------------------------------------------------


def test_parse_meta_accepts_what_the_backend_writes_and_ignores_the_owner():
    drive_id = uuid.uuid4()
    meta = parse_meta(_meta(drive_id), drive_id)
    assert meta is not None
    assert (meta.title, meta.key_nonce, meta.counter_nonce) == ("Photos", KEY_NONCE, COUNTER_NONCE)
    assert meta.created_at.year == 2026
    assert not hasattr(meta, "owner")


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        _meta(uuid.uuid4()),  # the id inside differs from the prefix it was found under
        b'{"driveId": "x"}',
    ],
)
def test_parse_meta_rejects_garbage_and_a_foreign_id(raw):
    assert parse_meta(raw, uuid.uuid4()) is None


@pytest.mark.parametrize("override", [{"title": ""}, {"title": "x" * 300}, {"keyNonce": ""}, {"counterNonce": None}])
def test_parse_meta_rejects_bad_fields(override):
    drive_id = uuid.uuid4()
    assert parse_meta(_meta(drive_id, **override), drive_id) is None


def test_parse_meta_survives_a_bad_creation_date():
    drive_id = uuid.uuid4()
    assert parse_meta(_meta(drive_id, createdAt="yesterday"), drive_id) is not None


def test_plan_ops_log_takes_a_contiguous_log_once_even_when_covered_twice():
    # A batch 0-10 plus the not yet deleted chunks it merged, then a chunk 10-15.
    ranges = sorted([_r(0, 10), _r(0, 4), _r(4, 10), _r(10, 15)], key=lambda r: (r.from_offset, -r.to_offset))
    chosen, length = plan_ops_log(ranges)
    assert [(r.from_offset, r.to_offset) for r in chosen] == [(0, 10), (10, 15)]
    assert length == 15


def test_plan_ops_log_of_a_drive_without_operations_is_empty():
    assert plan_ops_log([]) == ([], 0)


@pytest.mark.parametrize("ranges", [[_r(0, 4), _r(5, 9)], [_r(2, 9)], [_r(0, 4), _r(4, 6), _r(8, 9)]])
def test_plan_ops_log_refuses_a_log_with_a_hole(ranges):
    assert plan_ops_log(ranges) is None


# ---- endpoints -------------------------------------------------------------


@pytest.fixture
async def infra():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")
    try:
        async with _s3() as s3:
            await s3.list_buckets()
    except Exception as exc:
        pytest.skip(f"MinIO not reachable: {exc}")


@pytest.fixture
async def client_as():
    def _make(user: CurrentUser):
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _make
    app.dependency_overrides.pop(get_current_user, None)


def _s3():
    return aioboto3.Session().client(
        "s3", endpoint_url=MINIO_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=MINIO_KEY, aws_secret_access_key=MINIO_SECRET, use_ssl=False,
    )


def _config(bucket: str, **overrides) -> dict:
    cfg = {
        "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": bucket,
        "accessKey": MINIO_KEY, "secretKey": MINIO_SECRET, "useSsl": False,
    }
    cfg.update(overrides)
    return cfg


async def _bucket() -> str:
    name = f"test-restore-{uuid.uuid4().hex[:12]}"
    async with _s3() as s3:
        await s3.create_bucket(Bucket=name)
    return name


async def _forget(drive_id: str) -> None:
    """Drops the row only, as a lost database would; the bucket keeps everything."""
    async with async_session_factory() as db:
        drive = await db.get(Drive, uuid.UUID(drive_id))
        await db.delete(drive)
        await db.commit()


async def _make_drive(c, bucket: str, ops: list[bytes], data: bytes) -> str:
    drive_id = (
        await c.post(
            "/drives",
            json={"title": "Photos", "keyNonce": KEY_NONCE, "counterNonce": COUNTER_NONCE,
                  "customConfig": _config(bucket)},
        )
    ).json()["id"]
    for op in ops:
        assert (await c.post(f"/ops/{drive_id}", content=op)).status_code == 204
    if data:
        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": len(data)})).json()
        resp = await c.post("/data", params={"sessionId": session["sessionId"], "start": 0}, content=data)
        assert resp.status_code in (200, 204)
    return drive_id


async def test_a_lost_drive_is_found_restored_and_readable(infra, client_as):
    bucket = await _bucket()
    ops = [b"first-op|", b"second-op|"]
    data = bytes(range(256)) * (BLOCK_SIZE // 256 + 40)  # one full block plus a partial one
    async with client_as(USER_A) as c:
        drive_id = await _make_drive(c, bucket, ops, data)
    await _forget(drive_id)

    async with client_as(USER_B) as c:
        scan = (await c.post("/drives/restore/scan", json={"customConfig": _config(bucket)})).json()
        assert scan["truncated"] is False
        [found] = scan["drives"]
        assert (found["driveId"], found["title"], found["status"]) == (drive_id, "Photos", "RESTORABLE")
        assert found["opsBytes"] == len(b"".join(ops))

        resp = await c.post("/drives/restore", json={"customConfig": _config(bucket), "driveIds": [drive_id]})
        assert resp.json()["results"] == [{"driveId": drive_id, "status": "RESTORED", "reason": None}]

        [info] = [d for d in (await c.get("/drives")).json() if d["id"] == drive_id]
        assert info["ownerUserId"] == USER_B.sub  # the restorer, not the owner named in meta.json
        assert info["ACL"] == {}
        assert (info["keyNonce"], info["counterNonce"]) == (KEY_NONCE, COUNTER_NONCE)

        assert (await c.get(f"/ops/{drive_id}/0")).content == b"".join(ops)
        targets = (await c.get(f"/drives/{drive_id}/storage-targets")).json()
        assert [(t["role"], t["status"]) for t in targets] == [("MASTER", "ACTIVE")]
        got = await c.get("/data", params={"driveId": drive_id, "start": 0, "end": len(data)})
        assert got.content == data

        # New uploads land after the existing blocks instead of on top of them.
        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 5})).json()
        assert session["byteOffset"] == len(data)

        # Already in the system now: it is not restored a second time, nor offered.
        scan = (await c.post("/drives/restore/scan", json={"customConfig": _config(bucket)})).json()
        assert [d["status"] for d in scan["drives"]] == ["ALREADY_EXISTS"]
        resp = await c.post("/drives/restore", json={"customConfig": _config(bucket), "driveIds": [drive_id]})
        assert resp.json()["results"][0]["status"] == "ALREADY_EXISTS"

    async with client_as(USER_A) as c:
        # The status does not reveal who holds the drive, and it is unchanged for its former owner.
        resp = await c.post("/drives/restore", json={"customConfig": _config(bucket), "driveIds": [drive_id]})
        assert resp.json()["results"] == [{"driveId": drive_id, "status": "ALREADY_EXISTS", "reason": None}]
        assert "ownerUserId" not in resp.text and USER_B.sub not in resp.text


async def test_a_drive_with_a_hole_in_its_operation_log_is_not_restored(infra, client_as):
    bucket = await _bucket()
    async with client_as(USER_A) as c:
        drive_id = await _make_drive(c, bucket, [b"aaaa", b"bbbb", b"cccc"], b"")
    await _forget(drive_id)
    async with _s3() as s3:
        await s3.delete_object(Bucket=bucket, Key=f"ops/{drive_id}/{4:020d}-{8:020d}.bin")

    async with client_as(USER_B) as c:
        [found] = (await c.post("/drives/restore/scan", json={"customConfig": _config(bucket)})).json()["drives"]
        assert (found["status"], found["reason"]) == ("DAMAGED", "OPS_GAP")
        resp = await c.post("/drives/restore", json={"customConfig": _config(bucket), "driveIds": [drive_id]})
        assert resp.json()["results"] == [{"driveId": drive_id, "status": "DAMAGED", "reason": "OPS_GAP"}]
        assert drive_id not in [d["id"] for d in (await c.get("/drives")).json()]


async def test_a_drive_without_a_valid_meta_is_reported_and_an_unknown_id_is_not_found(infra, client_as):
    bucket = await _bucket()
    broken = uuid.uuid4()
    async with _s3() as s3:
        await s3.put_object(Bucket=bucket, Key=f"drives/{broken}/meta.json", Body=b"{}")
        await s3.put_object(Bucket=bucket, Key=f"drives/{uuid.uuid4()}/other.bin", Body=b"x")
        await s3.put_object(Bucket=bucket, Key="drives/not-a-uuid/meta.json", Body=b"{}")

    async with client_as(USER_B) as c:
        scan = (await c.post("/drives/restore/scan", json={"customConfig": _config(bucket)})).json()
        by_id = {d["driveId"]: d for d in scan["drives"]}
        assert by_id[str(broken)]["reason"] == "META_INVALID"
        assert "not-a-uuid" not in json.dumps(scan)
        assert all(d["status"] == "DAMAGED" for d in scan["drives"])

        stranger = str(uuid.uuid4())
        resp = await c.post("/drives/restore", json={"customConfig": _config(bucket), "driveIds": [stranger]})
        assert resp.json()["results"][0]["status"] == "NOT_FOUND"


async def test_storage_problems_are_explained_without_echoing_the_connection(infra, client_as):
    bucket = await _bucket()
    async with client_as(USER_B) as c:
        for cfg, reason in [
            (_config(bucket, endpointUrl="http://localhost:1"), "UNREACHABLE"),
            (_config(bucket, secretKey="wrong-secret-value"), "ACCESS_DENIED"),
            (_config(f"missing-{uuid.uuid4().hex[:12]}"), "BUCKET_NOT_FOUND"),
        ]:
            resp = await c.post("/drives/restore/scan", json={"customConfig": cfg})
            assert resp.status_code == 502
            detail = resp.json()["detail"]
            assert (detail["code"], detail["reason"]) == ("STORAGE_CONNECTION_FAILED", reason)
            for secret in ("localhost:1", cfg["bucket"], "wrong-secret-value", MINIO_KEY):
                assert secret not in resp.text
