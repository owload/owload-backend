"""End-to-end tests against a real Postgres + MinIO (docker-compose.yml).
Auth is stubbed via FastAPI dependency override — no live Keycloak needed.
Skips automatically if the database isn't reachable (e.g. docker compose not up).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import aioboto3
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive, DriveStorageTarget, S3Preset, S3Tier, StorageTargetRole, StorageTargetStatus
from app.db.session import async_session_factory, engine
from app.main import app
from app.services.backfill import BackfillService
from app.services.compaction import OpsCompactionService, reconstruct_ops_from_backup
from app.services.secrets import encrypt_secret
from app.services.storage import BLOCK_SIZE
from app.services.storage_targets import get_active_targets, get_master

MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"


async def _ensure_minio_bucket(bucket: str) -> None:
    session = aioboto3.Session()
    async with session.client(
        "s3", endpoint_url=MINIO_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=MINIO_ACCESS_KEY, aws_secret_access_key=MINIO_SECRET_KEY, use_ssl=False,
    ) as client:
        try:
            await client.create_bucket(Bucket=bucket)
        except client.exceptions.BucketAlreadyOwnedByYou:
            pass


async def _minio_get_object(bucket: str, key: str) -> bytes | None:
    session = aioboto3.Session()
    async with session.client(
        "s3", endpoint_url=MINIO_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=MINIO_ACCESS_KEY, aws_secret_access_key=MINIO_SECRET_KEY, use_ssl=False,
    ) as client:
        try:
            resp = await client.get_object(Bucket=bucket, Key=key)
        except client.exceptions.NoSuchKey:
            return None
        async with resp["Body"] as body:
            return await body.read()


async def _minio_list_keys(bucket: str, prefix: str) -> list[str]:
    session = aioboto3.Session()
    async with session.client(
        "s3", endpoint_url=MINIO_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=MINIO_ACCESS_KEY, aws_secret_access_key=MINIO_SECRET_KEY, use_ssl=False,
    ) as client:
        resp = await client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        return [o["Key"] for o in resp.get("Contents", [])]

pytestmark = pytest.mark.asyncio

USER_A = CurrentUser(sub="test-user-a", username="alice-test", email="alice-test@example.com")
USER_B = CurrentUser(sub="test-user-b", username="bob-test", email="bob-test@example.com")


@pytest.fixture(autouse=True)
async def _require_infra():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Postgres not reachable, skipping integration tests: {exc}")


@pytest.fixture
async def client_as():
    def _make(user: CurrentUser):
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield _make
    app.dependency_overrides.pop(get_current_user, None)


DEFAULT_AVATAR = {"kind": "default", "preset": None, "version": None}


async def test_userinfo_lifecycle(client_as):
    user = CurrentUser(sub=f"u-{uuid.uuid4()}", username="t", email=f"{uuid.uuid4()}@example.com")
    async with client_as(user) as c:
        # The profile is created on the first call; after a plain registration the token has no name.
        resp = await c.get("/userinfo")
        assert resp.status_code == 200
        body = resp.json()
        member_since = datetime.fromisoformat(body.pop("memberSince"))
        assert abs(datetime.now(timezone.utc) - member_since) < timedelta(minutes=5)
        assert body.pop("lastSeen") is None
        assert body == {"userId": user.sub, "email": user.email, "name": "", "avatar": DEFAULT_AVATAR}

        resp = await c.post("/userinfo", json={"name": "  Test User  "})
        assert resp.status_code == 204

        resp = await c.get("/userinfo")
        assert resp.status_code == 200
        body = resp.json()
        body.pop("memberSince")
        body.pop("lastSeen")
        assert body == {"userId": user.sub, "email": user.email, "name": "Test User", "avatar": DEFAULT_AVATAR}

        resp = await c.get(f"/userinfo/search/{user.email}")
        assert resp.status_code == 200
        assert [u["userId"] for u in resp.json()] == [user.sub]


async def test_userinfo_name_comes_from_the_identity_provider_and_email_cannot_be_changed(client_as):
    """The name is taken from the token (Google, SSO) while the stored name is empty and never overwrites a name the
    user set; the email always follows the token and the request cannot change it."""
    sub = f"u-{uuid.uuid4()}"
    email = f"{uuid.uuid4()}@example.com"
    sso = CurrentUser(sub=sub, username="t", email=email, name="Alex Morgan")
    async with client_as(sso) as c:
        resp = await c.get("/userinfo")
        assert resp.json()["name"] == "Alex Morgan"

        resp = await c.post("/userinfo", json={"name": "Alex M.", "email": "other@example.com"})
        assert resp.status_code == 204

    # The next sign-in carries the provider's name again, and a new address.
    later = CurrentUser(sub=sub, username="t", email=f"new-{email}", name="Alex Morgan")
    async with client_as(later) as c:
        body = (await c.get("/userinfo")).json()
        assert body["name"] == "Alex M."
        assert body["email"] == f"new-{email}"

        # An empty name clears it; the next sign-in fills it from the provider again.
        assert (await c.post("/userinfo", json={"name": ""})).status_code == 204
        assert (await c.get("/userinfo")).json()["name"] == "Alex Morgan"

        assert (await c.post("/userinfo", json={"name": "x" * 256})).status_code == 422


def _picture(width: int, height: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new(mode, (width, height)).save(out, format=fmt)
    return out.getvalue()


async def test_userinfo_avatar(client_as):
    """The picture of the profile: a default until chosen, then a preset or an uploaded image that the server scales down
    to a small square JPEG; a bad or oversized file is refused."""
    import io

    from PIL import Image

    user = CurrentUser(sub=f"u-{uuid.uuid4()}", username="t", email=f"{uuid.uuid4()}@example.com")
    async with client_as(user) as c:
        assert (await c.get("/userinfo")).json()["avatar"] == DEFAULT_AVATAR
        assert (await c.get("/userinfo/avatar")).status_code == 404

        assert (await c.put("/userinfo/avatar/preset", json={"preset": "rings"})).status_code == 204
        assert (await c.get("/userinfo")).json()["avatar"] == {"kind": "preset", "preset": "rings", "version": None}
        assert (await c.put("/userinfo/avatar/preset", json={"preset": "sunny"})).status_code == 422

        # A wide photo is cropped to a square and scaled down; the stored picture is always a 256 px JPEG.
        assert (await c.put("/userinfo/avatar/image", content=_picture(1200, 600))).status_code == 204
        avatar = (await c.get("/userinfo")).json()["avatar"]
        assert avatar["kind"] == "upload" and avatar["preset"] is None and avatar["version"]
        resp = await c.get("/userinfo/avatar")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["x-content-type-options"] == "nosniff"
        stored = Image.open(io.BytesIO(resp.content))
        assert stored.format == "JPEG" and stored.size == (256, 256)

        # A picture with transparency, and a WebP, are accepted too.
        assert (await c.put("/userinfo/avatar/image", content=_picture(64, 64, "PNG", "RGBA"))).status_code == 204
        assert (await c.put("/userinfo/avatar/image", content=_picture(300, 300, "WEBP"))).status_code == 204

        # Not an image; a broken one; too many bytes; too many pixels (a small file that unpacks to a huge picture).
        assert (await c.put("/userinfo/avatar/image", content=b"<svg onload=alert(1)>")).status_code == 415
        assert (await c.put("/userinfo/avatar/image", content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)).status_code == 415
        assert (await c.put("/userinfo/avatar/image", content=b"\xff\xd8\xff" + b"\x00" * (10 * 1024 * 1024))).status_code == 413
        assert (await c.put("/userinfo/avatar/image", content=_picture(9000, 9000, "PNG", "1"))).status_code == 413
        assert Image.open(io.BytesIO((await c.get("/userinfo/avatar")).content)).size == (256, 256)

        # A preset replaces the upload; reset goes back to the default.
        assert (await c.put("/userinfo/avatar/preset", json={"preset": "moon"})).status_code == 204
        assert (await c.get("/userinfo/avatar")).status_code == 404
        assert (await c.delete("/userinfo/avatar")).status_code == 204
        assert (await c.get("/userinfo")).json()["avatar"]["kind"] == "default"


CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
SAFARI_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"


async def test_account_activity_and_sessions(client_as):
    """A new session records a sign-in with the browser and the address; profile changes are recorded; the sessions are listed
    with the current one marked; `lastSeen` is when the user was last here before; a session signed out from another device is
    refused by the backend; another user's session is not reachable."""
    uid = f"u-{uuid.uuid4()}"
    laptop = CurrentUser(sub=uid, username="t", email=f"{uuid.uuid4()}@example.com", session_id=f"sid-{uuid.uuid4()}")
    phone = CurrentUser(sub=uid, username="t", email=laptop.email, session_id=f"sid-{uuid.uuid4()}")
    stranger = CurrentUser(sub=f"u-{uuid.uuid4()}", username="s", email=f"{uuid.uuid4()}@example.com", session_id=f"sid-{uuid.uuid4()}")
    mac, ios = {"user-agent": CHROME_MAC}, {"user-agent": SAFARI_IPHONE}

    async with client_as(laptop) as c:
        first = (await c.get("/userinfo", headers=mac)).json()
        assert first["lastSeen"] is None  # the very first visit
        assert (await c.post("/userinfo", json={"name": "Alex"}, headers=mac)).status_code == 204
        assert (await c.put("/userinfo/avatar/preset", json={"preset": "moon"}, headers=mac)).status_code == 204

        events = (await c.get("/userinfo/activity", headers=mac)).json()["items"]
        assert [e["kind"] for e in events] == ["picture_changed", "name_changed", "sign_in"]
        assert events[-1]["device"] == "Chrome on macOS" and events[-1]["ip"] == "127.0.0.1" and events[-1]["currentSession"] is True

        sessions = (await c.get("/userinfo/sessions", headers=mac)).json()
        assert [(s["device"], s["mobile"], s["current"]) for s in sessions] == [("Chrome on macOS", False, True)]

    async with client_as(phone) as c:
        second = (await c.get("/userinfo", headers=ios)).json()
        assert second["lastSeen"] is not None  # the laptop was here before
        # Parallel first calls of a visit record one sign-in, not two.
        await c.get("/userinfo", headers=ios)
        sessions = (await c.get("/userinfo/sessions", headers=ios)).json()
        assert {(s["device"], s["mobile"], s["current"]) for s in sessions} == {("Chrome on macOS", False, False), ("Safari on iOS", True, True)}
        laptop_id = next(s["id"] for s in sessions if not s["current"])
        sign_ins = [e for e in (await c.get("/userinfo/activity", headers=ios)).json()["items"] if e["kind"] == "sign_in"]
        assert len(sign_ins) == 2

    async with client_as(stranger) as c:
        assert (await c.delete(f"/userinfo/sessions/{laptop_id}")).status_code == 404

    async with client_as(phone) as c:
        assert (await c.delete(f"/userinfo/sessions/{phone.session_id}", headers=ios)).status_code == 400
        assert (await c.delete(f"/userinfo/sessions/{laptop_id}", headers=ios)).status_code == 204
        sessions = (await c.get("/userinfo/sessions", headers=ios)).json()
        assert [s["current"] for s in sessions] == [True]

    # The signed-out session is refused from now on, on any route.
    async with client_as(laptop) as c:
        refused = await c.get("/userinfo", headers=mac)
        assert refused.status_code == 401 and refused.json()["detail"] == "Session was signed out"
        assert (await c.get("/drives", headers=mac)).status_code == 401

    async with client_as(phone) as c:
        # Signing out on this device ends the session and records it.
        assert (await c.delete("/userinfo/sessions/current", headers=ios)).status_code == 204
        events = (await c.get("/userinfo/activity", headers=ios)).json()["items"]
        assert [e["kind"] for e in events][:2] == ["sign_out", "session_revoked"]
        assert (await c.get("/userinfo/sessions", headers=ios)).json() == []


async def test_activity_paging_and_signing_out_other_sessions(client_as):
    uid = f"u-{uuid.uuid4()}"
    users = [CurrentUser(sub=uid, username="t", email=f"{uid}@example.com", session_id=f"sid-{uuid.uuid4()}") for _ in range(3)]
    for user in users:
        async with client_as(user) as c:
            assert (await c.get("/userinfo")).status_code == 200

    async with client_as(users[0]) as c:
        page = (await c.get("/userinfo/activity", params={"limit": 2})).json()
        assert len(page["items"]) == 2 and page["next"] is not None
        rest = (await c.get("/userinfo/activity", params={"limit": 2, "before": page["next"]})).json()
        assert len(rest["items"]) == 1 and rest["next"] is None
        assert {e["id"] for e in page["items"]}.isdisjoint(e["id"] for e in rest["items"])

        # Only the kinds asked for.
        only = (await c.get("/userinfo/activity", params={"kind": ["sign_in", "sign_out"]})).json()["items"]
        assert [e["kind"] for e in only] == ["sign_in"] * 3
        assert (await c.get("/userinfo/activity", params={"kind": "name_changed"})).json() == {"items": [], "next": None}

        assert (await c.delete("/userinfo/sessions/others")).status_code == 204
        assert [s["current"] for s in (await c.get("/userinfo/sessions")).json()] == [True]
        assert (await c.get("/userinfo/activity")).json()["items"][0]["kind"] == "other_sessions_revoked"

    async with client_as(users[1]) as c:
        assert (await c.get("/userinfo")).status_code == 401


async def test_delete_drive_owner_only_and_respects_custom_target_choices(client_as):
    """Epic 0015 task 0001: DELETE /drives/{driveId} — owner only (403 for
    an ACL member, 404 for a total stranger, matching decisions/0013's
    "more destructive than ACL-granted access" reasoning). Preset targets
    are always wiped; each custom target needs the owner's explicit
    deleteData choice, and a missing/extra decision is rejected outright."""
    preset_bucket = f"test-deldrive-preset-{uuid.uuid4().hex[:12]}"
    keep_bucket = f"test-deldrive-keep-{uuid.uuid4().hex[:12]}"
    wipe_bucket = f"test-deldrive-wipe-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(preset_bucket)
    await _ensure_minio_bucket(keep_bucket)
    await _ensure_minio_bucket(wipe_bucket)

    async with async_session_factory() as db:
        preset = S3Preset(
            label=f"test-deldrive-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=preset_bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        db.add(preset)
        await db.commit()
        preset_id = preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post(
                "/drives",
                json={
                    "title": "Delete-drive test", "keyNonce": "aa", "counterNonce": "bb",
                    "presetId": str(preset_id),
                },
            )
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        keep_target = (
            await c.post(
                base,
                json={
                    "customConfig": {
                        "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": keep_bucket,
                        "accessKey": MINIO_ACCESS_KEY, "secretKey": MINIO_SECRET_KEY, "useSsl": False,
                    }
                },
            )
        ).json()
        wipe_target = (
            await c.post(
                base,
                json={
                    "customConfig": {
                        "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": wipe_bucket,
                        "accessKey": MINIO_ACCESS_KEY, "secretKey": MINIO_SECRET_KEY, "useSsl": False,
                    }
                },
            )
        ).json()

        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 5})).json()
        resp = await c.post("/data", params={"sessionId": session["sessionId"], "start": 0}, content=b"hello")
        assert resp.status_code == 204

    # ACL WRITE access doesn't grant delete rights (owner-only).
    async with async_session_factory() as db:
        row = await db.get(Drive, uuid.UUID(drive_id))
        row.acl = {USER_B.sub: ["READ", "WRITE"]}
        await db.commit()

    async with client_as(USER_B) as c:
        resp = await c.request("DELETE", f"/drives/{drive_id}", json={"customTargetDecisions": []})
        assert resp.status_code == 403

    stranger = CurrentUser(sub=f"stranger-{uuid.uuid4()}", username="s", email=f"{uuid.uuid4()}@example.com")
    async with client_as(stranger) as c:
        resp = await c.request("DELETE", f"/drives/{drive_id}", json={"customTargetDecisions": []})
        assert resp.status_code == 404

    async with client_as(USER_A) as c:
        # Missing a decision for one of the two custom targets -> 400.
        resp = await c.request(
            "DELETE",
            f"/drives/{drive_id}",
            json={"customTargetDecisions": [{"targetId": keep_target["id"], "deleteData": False}]},
        )
        assert resp.status_code == 400

        # A decision for a target that isn't this drive's -> 400.
        resp = await c.request(
            "DELETE",
            f"/drives/{drive_id}",
            json={
                "customTargetDecisions": [
                    {"targetId": keep_target["id"], "deleteData": False},
                    {"targetId": wipe_target["id"], "deleteData": True},
                    {"targetId": str(uuid.uuid4()), "deleteData": True},
                ]
            },
        )
        assert resp.status_code == 400

        resp = await c.request(
            "DELETE",
            f"/drives/{drive_id}",
            json={
                "customTargetDecisions": [
                    {"targetId": keep_target["id"], "deleteData": False},
                    {"targetId": wipe_target["id"], "deleteData": True},
                ]
            },
        )
        assert resp.status_code == 204

    async with async_session_factory() as db:
        assert await db.get(Drive, uuid.UUID(drive_id)) is None
        remaining = (
            await db.execute(select(DriveStorageTarget).where(DriveStorageTarget.drive_id == uuid.UUID(drive_id)))
        ).scalars().all()
        assert remaining == []

    key = f"data/{drive_id}/000000000000"
    assert await _minio_get_object(preset_bucket, key) is None  # preset: always wiped
    assert await _minio_get_object(wipe_bucket, key) is None  # deleteData=true
    assert await _minio_get_object(keep_bucket, key) == b"hello"  # deleteData=false -> preserved


async def test_delete_drive_force_completes_despite_unreachable_custom_target(client_as):
    """If a custom target's S3 has simply gone away (host down, credentials
    revoked — it's the owner's own infrastructure, not ours to guarantee),
    the purge can never succeed. Without force=true that permanently blocks
    deleting the drive at all; force=true accepts the orphaned-data risk and
    deletes it anyway."""
    async with async_session_factory() as db:
        bad_preset = S3Preset(
            label=f"test-unreachable-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url="http://localhost:1", region="us-east-1", bucket="whatever",
            access_key="ak", secret_key=encrypt_secret("sk"), use_ssl=False,
        )
        db.add(bad_preset)
        await db.commit()
        bad_preset_id = bad_preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Unreachable-target drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        resp = await c.post(
            f"/drives/{drive_id}/storage-targets",
            json={
                "customConfig": {
                    "endpointUrl": "http://localhost:1", "region": "us-east-1", "bucket": "whatever",
                    "accessKey": "ak", "secretKey": "sk", "useSsl": False,
                }
            },
        )
        assert resp.status_code == 201
        bad_target = resp.json()

        resp = await c.request(
            "DELETE",
            f"/drives/{drive_id}",
            json={"customTargetDecisions": [{"targetId": bad_target["id"], "deleteData": True}]},
        )
        assert resp.status_code == 502

        async with async_session_factory() as db:
            assert await db.get(Drive, uuid.UUID(drive_id)) is not None  # not deleted

        resp = await c.request(
            "DELETE",
            f"/drives/{drive_id}",
            params={"force": "true"},
            json={"customTargetDecisions": [{"targetId": bad_target["id"], "deleteData": True}]},
        )
        assert resp.status_code == 204

    async with async_session_factory() as db:
        assert await db.get(Drive, uuid.UUID(drive_id)) is None

    # The unrelated bad_preset row is untouched by any of this.
    async with async_session_factory() as db:
        assert await db.get(S3Preset, bad_preset_id) is not None


async def test_remove_storage_target_force_completes_despite_unreachable_target(client_as):
    """Same escape hatch as drive deletion, for detaching a single slave
    target (owload-docs/epics/0012 task 0005) whose S3 is unreachable."""
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Unreachable slave drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        resp = await c.post(
            base,
            json={
                "customConfig": {
                    "endpointUrl": "http://localhost:1", "region": "us-east-1", "bucket": "whatever",
                    "accessKey": "ak", "secretKey": "sk", "useSsl": False,
                }
            },
        )
        assert resp.status_code == 201
        bad_target = resp.json()

        resp = await c.delete(f"{base}/{bad_target['id']}", params={"confirm": "true"})
        assert resp.status_code == 502

        async with async_session_factory() as db:
            row = await db.get(DriveStorageTarget, uuid.UUID(bad_target["id"]))
            assert row is not None
            assert row.status == "REMOVING"  # stays REMOVING, safe to retry

        resp = await c.delete(f"{base}/{bad_target['id']}", params={"confirm": "true", "force": "true"})
        assert resp.status_code == 204

        async with async_session_factory() as db:
            assert await db.get(DriveStorageTarget, uuid.UUID(bad_target["id"])) is None


async def test_drive_create_get_list_and_isolation(client_as):
    async with client_as(USER_A) as c:
        resp = await c.post("/drives", json={"title": "Drive A", "keyNonce": "aa", "counterNonce": "bb"})
        assert resp.status_code == 201
        drive = resp.json()
        assert drive["ownerUserId"] == USER_A.sub
        assert drive["ACL"] == {}
        assert isinstance(drive["createdTimestamp"], int)

        resp = await c.get(f"/drives/{drive['id']}")
        assert resp.status_code == 200
        assert resp.json() == drive

        resp = await c.get("/drives")
        assert resp.status_code == 200
        assert any(d["id"] == drive["id"] for d in resp.json())

    async with client_as(USER_B) as c:
        resp = await c.get(f"/drives/{drive['id']}")
        assert resp.status_code == 404

    async with client_as(USER_A) as c:
        resp = await c.get(f"/drives/{uuid.uuid4()}")
        assert resp.status_code == 404


async def test_list_s3_presets_exposes_no_secrets(client_as):
    async with async_session_factory() as db:
        preset = S3Preset(
            label=f"test-list-{uuid.uuid4()}",
            tier=S3Tier.COLD,
            region="us-east-1",
            bucket="some-bucket",
            access_key="ak",
            secret_key=encrypt_secret("sk"),
            use_ssl=True,
        )
        db.add(preset)
        await db.commit()
        preset_id = preset.id

    async with client_as(USER_A) as c:
        resp = await c.get("/s3-presets")
        assert resp.status_code == 200
        by_id = {p["id"]: p for p in resp.json()}
        assert str(preset_id) in by_id
        entry = by_id[str(preset_id)]
        assert entry["tier"] == "cold"  # lowercased to match the frontend's S3PresetTier union
        assert set(entry.keys()) == {"id", "label", "tier"}  # no bucket/access_key/secret_key leak


async def test_s3_preset_connection_test(client_as):
    """Epic 0012 task 0009: presets are admin-curated but that doesn't mean
    they're reachable right now — POST /s3-presets/{id}/test-connection
    checks a bare preset directly, before it's ever attached to a drive."""
    bucket = f"test-presetconn-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(bucket)

    async with async_session_factory() as db:
        ok_preset = S3Preset(
            label=f"test-presetconn-ok-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        bad_preset = S3Preset(
            label=f"test-presetconn-bad-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url="http://localhost:1", region="us-east-1", bucket=bucket,
            access_key="ak", secret_key=encrypt_secret("sk"), use_ssl=False,
        )
        db.add_all([ok_preset, bad_preset])
        await db.commit()
        ok_preset_id, bad_preset_id = ok_preset.id, bad_preset.id

    async with client_as(USER_A) as c:
        resp = await c.post(f"/s3-presets/{ok_preset_id}/test-connection")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "error": None}

        resp = await c.post(f"/s3-presets/{bad_preset_id}/test-connection")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]

        resp = await c.post(f"/s3-presets/{uuid.uuid4()}/test-connection")
        assert resp.status_code == 404


async def test_drive_creation_target_selection(client_as):
    # Real S3_* creds/bucket from settings — the route actually writes
    # drives/{id}/meta.json to whichever target is selected, so a fake
    # bucket/access key would fail the real S3 call, not just the DB check.
    settings = get_settings()

    async with async_session_factory() as db:
        cold_preset = S3Preset(
            label=f"test-cold-{uuid.uuid4()}",
            tier=S3Tier.COLD,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=encrypt_secret(settings.s3_secret_key),
            use_ssl=settings.s3_use_ssl,
        )
        hot_preset = S3Preset(
            label=f"test-hot-{uuid.uuid4()}",
            tier=S3Tier.HOT,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=encrypt_secret(settings.s3_secret_key),
            use_ssl=settings.s3_use_ssl,
        )
        db.add_all([cold_preset, hot_preset])
        await db.commit()
        cold_preset_id, hot_preset_id = cold_preset.id, hot_preset.id

    async with client_as(USER_A) as c:
        # Cold tier can't back a drive's initial (master) target.
        resp = await c.post(
            "/drives",
            json={
                "title": "Cold-preset drive",
                "keyNonce": "aa",
                "counterNonce": "bb",
                "presetId": str(cold_preset_id),
            },
        )
        assert resp.status_code == 400

        # A nonexistent preset is a 404, not treated as "no selection".
        resp = await c.post(
            "/drives",
            json={
                "title": "Missing-preset drive",
                "keyNonce": "aa",
                "counterNonce": "bb",
                "presetId": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 404

        resp = await c.post(
            "/drives",
            json={
                "title": "Hot-preset drive",
                "keyNonce": "aa",
                "counterNonce": "bb",
                "presetId": str(hot_preset_id),
            },
        )
        assert resp.status_code == 201
        hot_drive_id = uuid.UUID(resp.json()["id"])

        resp = await c.post(
            "/drives",
            json={
                "title": "Custom-config drive",
                "keyNonce": "aa",
                "counterNonce": "bb",
                "customConfig": {
                    "endpointUrl": settings.s3_endpoint_url,
                    "region": settings.s3_region,
                    "bucket": settings.s3_bucket,
                    "accessKey": settings.s3_access_key,
                    "secretKey": settings.s3_secret_key,
                    "useSsl": settings.s3_use_ssl,
                },
            },
        )
        assert resp.status_code == 201
        custom_drive_id = uuid.UUID(resp.json()["id"])

        # Both presetId and customConfig at once is rejected before touching the DB.
        resp = await c.post(
            "/drives",
            json={
                "title": "Conflicting drive",
                "keyNonce": "aa",
                "counterNonce": "bb",
                "presetId": str(hot_preset_id),
                "customConfig": {"region": "us-east-1", "bucket": "b", "accessKey": "a", "secretKey": "s"},
            },
        )
        assert resp.status_code == 422

    async with async_session_factory() as db:
        targets = (
            await db.execute(select(DriveStorageTarget).where(DriveStorageTarget.drive_id == hot_drive_id))
        ).scalars().all()
        assert len(targets) == 1
        assert targets[0].role == "MASTER"
        assert targets[0].status == "ACTIVE"
        assert targets[0].preset_id == hot_preset_id

        targets = (
            await db.execute(select(DriveStorageTarget).where(DriveStorageTarget.drive_id == custom_drive_id))
        ).scalars().all()
        assert len(targets) == 1
        assert targets[0].role == "MASTER"
        assert targets[0].status == "ACTIVE"
        assert targets[0].preset_id is None
        assert targets[0].custom_config["bucket"] == settings.s3_bucket
        assert targets[0].custom_config["secret_key"] != settings.s3_secret_key  # stored Fernet-encrypted


async def test_new_slave_on_empty_drive_is_immediately_active(client_as):
    """A slave added to a drive with zero data/ops history has nothing to
    backfill — master and the new slave are already trivially identical
    (both empty) — so it should start ACTIVE, not stuck PROVISIONING
    forever waiting on a backfill worker that doesn't exist yet
    (owload-docs/epics/0013-s3-target-backfill-worker)."""
    bucket = f"test-emptydrive-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(bucket)

    async with async_session_factory() as db:
        preset = S3Preset(
            label=f"test-emptydrive-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        db.add(preset)
        await db.commit()
        preset_id = preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Empty drive for slave", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        resp = await c.post(base, json={"presetId": str(preset_id)})
        assert resp.status_code == 201
        target = resp.json()
        assert target["status"] == "ACTIVE"

        # Immediately promotable — no need to wait on a nonexistent backfill worker.
        resp = await c.patch(f"{base}/{target['id']}/make-master")
        assert resp.status_code == 204


async def test_storage_target_connection_test_endpoints(client_as):
    """Epic 0012 task 0009: POST /storage-targets/test (arbitrary, unsaved
    config) and POST /drives/{driveId}/storage-targets/{id}/test-connection
    (already-saved target) — both always 200, {ok, error} describes the
    actual result rather than a failed connection being an API error."""
    bucket = f"test-conntest-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(bucket)

    async with async_session_factory() as db:
        preset = S3Preset(
            label=f"test-conntest-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        bad_preset = S3Preset(
            label=f"test-conntest-bad-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url="http://localhost:1", region="us-east-1", bucket=bucket,
            access_key="ak", secret_key=encrypt_secret("sk"), use_ssl=False,
        )
        db.add_all([preset, bad_preset])
        await db.commit()
        preset_id, bad_preset_id = preset.id, bad_preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Conn-test drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        target = (await c.post(base, json={"presetId": str(preset_id)})).json()
        resp = await c.post(f"{base}/{target['id']}/test-connection")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "error": None}

        bad_target = (await c.post(base, json={"presetId": str(bad_preset_id)})).json()
        resp = await c.post(f"{base}/{bad_target['id']}/test-connection")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]

        # Arbitrary, not-yet-saved config (create-drive-page's "Test connection" button).
        resp = await c.post(
            "/storage-targets/test",
            json={
                "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": bucket,
                "accessKey": MINIO_ACCESS_KEY, "secretKey": MINIO_SECRET_KEY, "useSsl": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "error": None}

        resp = await c.post(
            "/storage-targets/test",
            json={
                "endpointUrl": "http://localhost:1", "region": "us-east-1", "bucket": bucket,
                "accessKey": "ak", "secretKey": "sk", "useSsl": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False


async def test_backfill_service_copies_existing_backlog_to_new_slave(client_as):
    """Epic 0013: BackfillService copies a PROVISIONING target's missing
    backlog (data blocks, ops chunks, drives/{id}/meta.json) from master,
    then flips it ACTIVE. Master and slave live on separate local MinIO
    buckets so the copy is verifiably real, not incidental (same bucket)."""
    master_bucket = f"test-backfill-master-{uuid.uuid4().hex[:12]}"
    slave_bucket = f"test-backfill-slave-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(master_bucket)
    await _ensure_minio_bucket(slave_bucket)

    async with async_session_factory() as db:
        slave_preset = S3Preset(
            label=f"test-backfill-slave-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=slave_bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        db.add(slave_preset)
        await db.commit()
        slave_preset_id = slave_preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post(
                "/drives",
                json={
                    "title": "Backfill test drive", "keyNonce": "aa", "counterNonce": "bb",
                    "customConfig": {
                        "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": master_bucket,
                        "accessKey": MINIO_ACCESS_KEY, "secretKey": MINIO_SECRET_KEY, "useSsl": False,
                    },
                },
            )
        ).json()
        drive_id = drive["id"]

        # Backlog written through master before any slave exists.
        resp = await c.post(f"/ops/{drive_id}", content=b"\x01\x02\x03")
        assert resp.status_code == 204
        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 10})).json()
        resp = await c.post("/data", params={"sessionId": session["sessionId"], "start": 0}, content=b"0123456789")
        assert resp.status_code == 204

        base = f"/drives/{drive_id}/storage-targets"
        resp = await c.post(base, json={"presetId": str(slave_preset_id)})
        assert resp.status_code == 201
        slave_target = resp.json()
        assert slave_target["status"] == "PROVISIONING"  # drive already had data at add-time

    slave_target_id = uuid.UUID(slave_target["id"])

    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(DriveStorageTarget)
                .options(selectinload(DriveStorageTarget.preset))
                .where(DriveStorageTarget.id == slave_target_id)
            )
        ).scalar_one()

        service = BackfillService()
        progress_before = await service.progress(db, row)
        assert progress_before["copied"] < progress_before["total"]

        result = await service.backfill_target(db, row)
        assert result["data"] == {"copied": 1, "total": 1}
        assert result["ops"] == {"copied": 1, "total": 1}
        assert result["drives"] == {"copied": 1, "total": 1}

    async with async_session_factory() as db:
        refreshed = await db.get(DriveStorageTarget, slave_target_id)
        assert refreshed.status == "ACTIVE"

    data_key = f"data/{drive_id}/000000000000"
    assert await _minio_get_object(slave_bucket, data_key) == b"0123456789"
    meta_bytes = await _minio_get_object(slave_bucket, f"drives/{drive_id}/meta.json")
    assert meta_bytes is not None
    assert json.loads(meta_bytes)["driveId"] == drive_id

    async with client_as(USER_A) as c:
        resp = await c.get(f"/drives/{drive_id}/storage-targets")
        assert resp.status_code == 200
        by_id = {t["id"]: t for t in resp.json()}
        assert by_id[str(slave_target_id)]["status"] == "ACTIVE"
        assert by_id[str(slave_target_id)]["backfillCopied"] is None  # only reported while PROVISIONING


async def test_add_storage_target_rejects_duplicate_physical_location(client_as):
    """Epic 0012 task 0011: two drive_storage_targets rows pointing at the
    same (endpoint_url, bucket) aren't independent copies of anything —
    deleting one would physically delete the other's "live" data too."""
    shared_bucket = f"test-duploc-{uuid.uuid4().hex[:12]}"
    other_bucket = f"test-duploc-other-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(shared_bucket)
    await _ensure_minio_bucket(other_bucket)

    async with async_session_factory() as db:
        master_preset = S3Preset(
            label=f"test-duploc-master-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=shared_bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        other_preset = S3Preset(
            label=f"test-duploc-otherpreset-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=other_bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        db.add_all([master_preset, other_preset])
        await db.commit()
        master_preset_id, other_preset_id = master_preset.id, other_preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post(
                "/drives",
                json={
                    "title": "Duplicate-location drive", "keyNonce": "aa", "counterNonce": "bb",
                    "presetId": str(master_preset_id),
                },
            )
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        # Same preset as master -> same physical location -> rejected.
        resp = await c.post(base, json={"presetId": str(master_preset_id)})
        assert resp.status_code == 400

        # Custom config pointing at the exact same (endpointUrl, bucket) as
        # the master preset -> can't dodge the check by going custom.
        resp = await c.post(
            base,
            json={
                "customConfig": {
                    "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": shared_bucket,
                    "accessKey": "different-ak", "secretKey": "different-sk", "useSsl": False,
                }
            },
        )
        assert resp.status_code == 400

        # A genuinely different location is fine.
        resp = await c.post(base, json={"presetId": str(other_preset_id)})
        assert resp.status_code == 201
        slave_target = resp.json()

        # Two custom configs at the same (endpointUrl, bucket) but different
        # credentials -> still the same physical location -> rejected.
        resp = await c.post(
            base,
            json={
                "customConfig": {
                    "endpointUrl": MINIO_ENDPOINT, "region": "us-east-1", "bucket": other_bucket,
                    "accessKey": "yet-another-ak", "secretKey": "yet-another-sk", "useSsl": False,
                }
            },
        )
        assert resp.status_code == 400

        # Sanity: still exactly master + the one real slave, nothing duplicated.
        resp = await c.get(base)
        listed = resp.json()
        assert len(listed) == 2
        assert slave_target["id"] in {t["id"] for t in listed}
        assert sum(1 for t in listed if t["role"] == "MASTER") == 1


async def test_storage_target_lifecycle_add_promote_and_remove(client_as):
    """Epic 0012 task 0005: POST/PATCH make-master/DELETE on
    /drives/{driveId}/storage-targets. Cold and hot slave presets live on
    separate local MinIO buckets (not the drive's real master bucket) so
    that deleting one target's data can be verified not to touch another's."""
    cold_bucket = f"test-cold-{uuid.uuid4().hex[:12]}"
    hot_bucket = f"test-hotslave-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(cold_bucket)
    await _ensure_minio_bucket(hot_bucket)

    async with async_session_factory() as db:
        cold_preset = S3Preset(
            label=f"test-cold-{uuid.uuid4()}", tier=S3Tier.COLD,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=cold_bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        hot_preset = S3Preset(
            label=f"test-hotslave-{uuid.uuid4()}", tier=S3Tier.HOT,
            endpoint_url=MINIO_ENDPOINT, region="us-east-1", bucket=hot_bucket,
            access_key=MINIO_ACCESS_KEY, secret_key=encrypt_secret(MINIO_SECRET_KEY), use_ssl=False,
        )
        db.add_all([cold_preset, hot_preset])
        await db.commit()
        cold_preset_id, hot_preset_id = cold_preset.id, hot_preset.id

    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Target lifecycle drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 5})).json()
        resp = await c.post("/data", params={"sessionId": session["sessionId"], "start": 0}, content=b"abcde")
        assert resp.status_code == 204

        # Slaves may use any tier (only the drive's initial/master target is hot-only).
        resp = await c.post(base, json={"presetId": str(cold_preset_id)})
        assert resp.status_code == 201
        cold_target = resp.json()
        assert cold_target["role"] == "SLAVE"
        assert cold_target["status"] == "PROVISIONING"
        assert cold_target["tier"] == "cold"
        assert cold_target["isCustom"] is False
        assert cold_target["presetId"] == str(cold_preset_id)

        # Can't promote a PROVISIONING target.
        resp = await c.patch(f"{base}/{cold_target['id']}/make-master")
        assert resp.status_code == 400

        # Simulate the (not-yet-built) backfill worker finishing: flip to ACTIVE directly.
        async with async_session_factory() as db:
            row = await db.get(DriveStorageTarget, uuid.UUID(cold_target["id"]))
            row.status = StorageTargetStatus.ACTIVE
            await db.commit()

        # Even ACTIVE, a cold-tier target can't become master.
        resp = await c.patch(f"{base}/{cold_target['id']}/make-master")
        assert resp.status_code == 400

        resp = await c.post(base, json={"presetId": str(hot_preset_id)})
        assert resp.status_code == 201
        hot_target = resp.json()
        assert hot_target["status"] == "PROVISIONING"
        assert hot_target["tier"] == "hot"
        assert hot_target["isCustom"] is False

        # A drive can have at most 2 slaves.
        resp = await c.post(base, json={"presetId": str(hot_preset_id)})
        assert resp.status_code == 400

        # GET lists all three targets (master + 2 slaves) in the exact
        # shape the frontend's DriveStorageTarget interface expects.
        resp = await c.get(base)
        assert resp.status_code == 200
        listed = resp.json()
        assert len(listed) == 3
        by_id = {t["id"]: t for t in listed}
        assert cold_target["id"] in by_id
        assert hot_target["id"] in by_id
        assert by_id[cold_target["id"]]["tier"] == "cold"
        assert by_id[hot_target["id"]]["tier"] == "hot"
        master_entry = next(t for t in listed if t["role"] == "MASTER")
        assert master_entry["status"] == "ACTIVE"
        assert master_entry["tier"] == "hot"

        # A write after both slaves joined must fan out to them too, even
        # before any backfill of the pre-existing "abcde" has happened.
        session2 = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 3})).json()
        resp = await c.post("/data", params={"sessionId": session2["sessionId"], "start": 0}, content=b"xyz")
        assert resp.status_code == 204

        key = f"data/{drive_id}/000000000000"
        assert await _minio_get_object(cold_bucket, key) == b"abcdexyz"
        assert await _minio_get_object(hot_bucket, key) == b"abcdexyz"

    # Can't delete the master this way.
    async with async_session_factory() as db:
        master = (
            await db.execute(
                select(DriveStorageTarget).where(
                    DriveStorageTarget.drive_id == uuid.UUID(drive_id),
                    DriveStorageTarget.role == StorageTargetRole.MASTER,
                )
            )
        ).scalar_one()
        master_id = str(master.id)

    async with client_as(USER_A) as c:
        resp = await c.delete(f"{base}/{master_id}")
        assert resp.status_code == 400

        # Deleting a slave requires confirm=true.
        resp = await c.delete(f"{base}/{cold_target['id']}")
        assert resp.status_code == 400

        resp = await c.delete(f"{base}/{cold_target['id']}", params={"confirm": "true"})
        assert resp.status_code == 204

        resp = await c.get(base)
        assert {t["id"] for t in resp.json()} == {master_id, hot_target["id"]}

    assert await _minio_get_object(cold_bucket, key) is None  # actually deleted from S3
    assert await _minio_get_object(hot_bucket, key) == b"abcdexyz"  # the other slave untouched

    async with async_session_factory() as db:
        remaining = (
            await db.execute(
                select(DriveStorageTarget).where(DriveStorageTarget.drive_id == uuid.UUID(drive_id))
            )
        ).scalars().all()
        assert {t.id for t in remaining} == {uuid.UUID(master_id), uuid.UUID(hot_target["id"])}

        # Promote the surviving (now-backfilled) hot slave to master.
        row = await db.get(DriveStorageTarget, uuid.UUID(hot_target["id"]))
        row.status = StorageTargetStatus.ACTIVE
        await db.commit()

    async with client_as(USER_A) as c:
        resp = await c.patch(f"{base}/{hot_target['id']}/make-master")
        assert resp.status_code == 204

    async with async_session_factory() as db:
        roles = {
            t.id: t.role
            for t in (
                await db.execute(
                    select(DriveStorageTarget).where(DriveStorageTarget.drive_id == uuid.UUID(drive_id))
                )
            ).scalars().all()
        }
        assert roles[uuid.UUID(hot_target["id"])] == StorageTargetRole.MASTER
        assert roles[uuid.UUID(master_id)] == StorageTargetRole.SLAVE


async def test_ops_append_and_slice_semantics(client_as):
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Ops drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        resp = await c.get(f"/ops/{drive_id}/0")
        assert resp.status_code == 200
        assert resp.content == b""

        resp = await c.post(f"/ops/{drive_id}", content=b"\x01\x02\x03")
        assert resp.status_code == 204
        resp = await c.post(f"/ops/{drive_id}", content=b"\x04\x05")
        assert resp.status_code == 204

        resp = await c.get(f"/ops/{drive_id}/0")
        assert resp.content == b"\x01\x02\x03\x04\x05"
        resp = await c.get(f"/ops/{drive_id}/2")
        assert resp.content == b"\x03\x04\x05"
        resp = await c.get(f"/ops/{drive_id}/100")
        assert resp.content == b""

        resp = await c.get(f"/ops/{drive_id}/-1")
        assert resp.status_code == 400

        resp = await c.get(f"/ops/{uuid.uuid4()}/0")
        assert resp.status_code == 404
        resp = await c.post(f"/ops/{uuid.uuid4()}", content=b"\x01")
        assert resp.status_code == 404


async def test_session_write_finalize_and_read_data(client_as):
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Data drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        resp = await c.post("/sessions", params={"driveId": drive_id, "byteLength": 100})
        assert resp.status_code == 201
        session = resp.json()
        assert session["byteLength"] == 100
        assert session["status"] == 0  # ACTIVE

        msg1 = b"Hello and one more time bonjour, my dear friend!"
        msg2 = b" It's good to see you again. Isn't it?"
        resp = await c.post(
            "/data", params={"sessionId": session["sessionId"], "start": 0}, content=msg1
        )
        assert resp.status_code == 204
        resp = await c.post(
            "/data", params={"sessionId": session["sessionId"], "start": len(msg1)}, content=msg2
        )
        assert resp.status_code == 204

        resp = await c.post(f"/sessions/finalize/{session['sessionId']}")
        assert resp.status_code == 204

        offset = session["byteOffset"]
        resp = await c.get(
            "/data",
            params={"driveId": drive_id, "start": offset, "end": offset + len(msg1) + len(msg2)},
        )
        assert resp.status_code == 200
        assert resp.content == msg1 + msg2

        # Unreserved-but-allocated tail of the session should read back as zeros.
        resp = await c.get(
            "/data",
            params={"driveId": drive_id, "start": offset + len(msg1) + len(msg2), "end": offset + 100},
        )
        assert resp.content == b"\x00" * (100 - len(msg1) - len(msg2))

        # Finalizing an already-finished session is rejected.
        resp = await c.post(f"/sessions/finalize/{session['sessionId']}")
        assert resp.status_code == 409


async def test_free_range_deletes_only_fully_covered_blocks(client_as):
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Delete test drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        # "File A": [0, 1_500_000) — fully covers block 0, and only partially
        # covers block 1 (block 1 is [BLOCK_SIZE, 2*BLOCK_SIZE)).
        file_a_len = 1_500_000
        assert file_a_len > BLOCK_SIZE
        session_a = (
            await c.post("/sessions", params={"driveId": drive_id, "byteLength": file_a_len})
        ).json()
        file_a_content = bytes([0xAA]) * file_a_len
        resp = await c.post(
            "/data", params={"sessionId": session_a["sessionId"], "start": 0}, content=file_a_content
        )
        assert resp.status_code == 204
        await c.post(f"/sessions/finalize/{session_a['sessionId']}")

        # "File B": next 500_000 bytes — shares block 1 with the tail of file A.
        file_b_len = 500_000
        session_b = (
            await c.post("/sessions", params={"driveId": drive_id, "byteLength": file_b_len})
        ).json()
        assert session_b["byteOffset"] == file_a_len
        file_b_content = bytes([0xBB]) * file_b_len
        resp = await c.post(
            "/data", params={"sessionId": session_b["sessionId"], "start": 0}, content=file_b_content
        )
        assert resp.status_code == 204
        await c.post(f"/sessions/finalize/{session_b['sessionId']}")

        # Sanity check before deletion.
        resp = await c.get("/data", params={"driveId": drive_id, "start": 0, "end": file_a_len})
        assert resp.content == file_a_content
        resp = await c.get(
            "/data", params={"driveId": drive_id, "start": file_a_len, "end": file_a_len + file_b_len}
        )
        assert resp.content == file_b_content

        # Delete file A.
        resp = await c.delete("/data", params={"driveId": drive_id, "start": 0, "end": file_a_len})
        assert resp.status_code == 204

        # Block 0 was exclusively file A's (fully covered by the freed range) -> gone, reads as zero.
        resp = await c.get("/data", params={"driveId": drive_id, "start": 0, "end": BLOCK_SIZE})
        assert resp.content == b"\x00" * BLOCK_SIZE

        # Block 1 was only partially covered (file A's tail + file B's head) -> left untouched,
        # so file B's still-live bytes read back exactly as written.
        resp = await c.get(
            "/data", params={"driveId": drive_id, "start": file_a_len, "end": file_a_len + file_b_len}
        )
        assert resp.content == file_b_content


async def test_allocated_ranges_reflects_deleted_blocks(client_as):
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Ranges test drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        # Two files sharing block 1, same layout as the free_range test above.
        file_a_len = 1_500_000
        session_a = (
            await c.post("/sessions", params={"driveId": drive_id, "byteLength": file_a_len})
        ).json()
        await c.post(
            "/data",
            params={"sessionId": session_a["sessionId"], "start": 0},
            content=bytes([0xAA]) * file_a_len,
        )
        await c.post(f"/sessions/finalize/{session_a['sessionId']}")

        file_b_len = 500_000
        session_b = (
            await c.post("/sessions", params={"driveId": drive_id, "byteLength": file_b_len})
        ).json()
        await c.post(
            "/data",
            params={"sessionId": session_b["sessionId"], "start": 0},
            content=bytes([0xBB]) * file_b_len,
        )
        await c.post(f"/sessions/finalize/{session_b['sessionId']}")

        # Before deletion: blocks 0 and 1 both exist -> one contiguous range [0, 2*BLOCK_SIZE).
        resp = await c.get("/data/ranges", params={"driveId": drive_id})
        assert resp.status_code == 200
        assert resp.json() == [{"start": 0, "end": 2 * BLOCK_SIZE}]

        # Delete file A -> block 0 (exclusively file A's) goes away, block 1 (shared) stays.
        resp = await c.delete("/data", params={"driveId": drive_id, "start": 0, "end": file_a_len})
        assert resp.status_code == 204

        resp = await c.get("/data/ranges", params={"driveId": drive_id})
        assert resp.status_code == 200
        assert resp.json() == [{"start": BLOCK_SIZE, "end": 2 * BLOCK_SIZE}]

        # A drive with no data at all has no allocated ranges.
        empty_drive = (
            await c.post("/drives", json={"title": "Empty drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        resp = await c.get("/data/ranges", params={"driveId": empty_drive["id"]})
        assert resp.status_code == 200
        assert resp.json() == []


async def test_drive_action_log_records_every_action_and_only_on_success(client_as):
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Audited drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        # CREATE_DRIVE
        resp = await c.post(f"/ops/{drive_id}", content=b"\x01\x02\x03")  # SAVE_OP
        assert resp.status_code == 204
        resp = await c.get(f"/ops/{drive_id}/0")  # GET_OPS — read, not logged
        assert resp.status_code == 200

        session = (
            await c.post("/sessions", params={"driveId": drive_id, "byteLength": 10})
        ).json()  # START_SESSION
        resp = await c.post(
            "/data", params={"sessionId": session["sessionId"], "start": 0}, content=b"0123456789"
        )  # SAVE_DATA — chunked write, not logged
        assert resp.status_code == 204
        resp = await c.get("/data", params={"driveId": drive_id, "start": 0, "end": 10})  # GET_DATA — read, not logged
        assert resp.status_code == 200
        resp = await c.post(f"/sessions/finalize/{session['sessionId']}")  # FINALIZE_SESSION
        assert resp.status_code == 204
        resp = await c.delete("/data", params={"driveId": drive_id, "start": 0, "end": 0})  # no-op DELETE_DATA
        assert resp.status_code == 204

        # A failed request (finalizing an already-finished session -> 409) must NOT be logged.
        resp = await c.post(f"/sessions/finalize/{session['sessionId']}")
        assert resp.status_code == 409

        resp = await c.get(f"/drives/{drive_id}/actions", params={"limit": 100})
        assert resp.status_code == 200
        entries = resp.json()

        actions_in_order = [e["action"] for e in entries]
        # Most recent first. GET_OPS/GET_DATA/SAVE_DATA never appear (not
        # logged at all), and the failed re-finalize doesn't appear either.
        assert actions_in_order == [
            "DELETE_DATA",
            "FINALIZE_SESSION",
            "START_SESSION",
            "SAVE_OP",
            "CREATE_DRIVE",
        ]
        assert all(e["driveId"] == drive_id for e in entries)
        assert all(e["userId"] == USER_A.sub for e in entries)

        save_op_entry = next(e for e in entries if e["action"] == "SAVE_OP")
        assert save_op_entry["attributes"] == {"start": 0, "end": 3}

        # seq is strictly decreasing (most recent first) and unique.
        seqs = [e["seq"] for e in entries]
        assert seqs == sorted(seqs, reverse=True)
        assert len(set(seqs)) == len(seqs)

        # Keyset pagination via `before`.
        resp = await c.get(f"/drives/{drive_id}/actions", params={"limit": 2})
        page1 = resp.json()
        assert [e["action"] for e in page1] == ["DELETE_DATA", "FINALIZE_SESSION"]
        resp = await c.get(f"/drives/{drive_id}/actions", params={"limit": 2, "before": page1[-1]["seq"]})
        page2 = resp.json()
        assert [e["action"] for e in page2] == ["START_SESSION", "SAVE_OP"]

    # Another user can't read this drive's action log.
    async with client_as(USER_B) as c:
        resp = await c.get(f"/drives/{drive_id}/actions")
        assert resp.status_code == 404


async def test_drive_actions_keyset_pagination_survives_concurrent_insert(client_as):
    """The scenario OFFSET pagination gets wrong: a row inserted between two
    page fetches must never cause a skip or a duplicate."""
    async with client_as(USER_A) as c:
        drive = (
            await c.post("/drives", json={"title": "Pagination stress drive", "keyNonce": "aa", "counterNonce": "bb"})
        ).json()
        drive_id = drive["id"]

        n = 210
        for i in range(n):
            resp = await c.post(f"/ops/{drive_id}", content=bytes([i % 256]))
            assert resp.status_code == 204

        # Ground truth: every row that exists *before* pagination starts
        # (fetched with its own full page walk — the route caps limit at 200).
        baseline: list[dict] = []
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor is not None:
                params["before"] = cursor
            page = (await c.get(f"/drives/{drive_id}/actions", params=params)).json()
            if not page:
                break
            baseline.extend(page)
            cursor = page[-1]["seq"]
        baseline_ids = {e["id"] for e in baseline}
        assert len(baseline_ids) == n + 1  # CREATE_DRIVE + n SAVE_OP

        seen_ids: list[str] = []
        before = None
        page_count = 0
        while True:
            params = {"limit": 50}
            if before is not None:
                params["before"] = before
            resp = await c.get(f"/drives/{drive_id}/actions", params=params)
            page = resp.json()
            if not page:
                break
            seen_ids.extend(e["id"] for e in page)
            before = page[-1]["seq"]
            page_count += 1

            # Insert a new row concurrently, between this page fetch and the next.
            # It's strictly newer than everything already paginated (we walk
            # newest -> oldest), so it correctly never appears in a later page —
            # that's not what's under test here. What must hold regardless is
            # that every *pre-existing* row still comes back exactly once.
            if page_count == 1:
                resp = await c.post(f"/ops/{drive_id}", content=b"\xff")
                assert resp.status_code == 204

        seen_baseline = [i for i in seen_ids if i in baseline_ids]
        assert len(seen_baseline) == len(baseline_ids)  # no skips, no duplicates
        assert set(seen_baseline) == baseline_ids


async def test_compaction_runs_on_every_target_and_survives_an_unreachable_one(client_as):
    """Epic 0012 task 0010: compaction must go through each of a drive's
    storage targets independently (every target holds its own copy of ops/),
    not just a single global bucket, and one unreachable target must not hold
    up the others. Master and slave live on separate local MinIO buckets so
    the per-target result is verifiable on each; a third slave points at a
    dead address."""
    master_bucket = f"test-compact-master-{uuid.uuid4().hex[:12]}"
    slave_bucket = f"test-compact-slave-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(master_bucket)
    await _ensure_minio_bucket(slave_bucket)

    def _custom(bucket: str, endpoint: str = MINIO_ENDPOINT) -> dict:
        return {
            "endpointUrl": endpoint, "region": "us-east-1", "bucket": bucket,
            "accessKey": MINIO_ACCESS_KEY, "secretKey": MINIO_SECRET_KEY, "useSsl": False,
        }

    chunks = [b"op-one|", b"op-two|", b"op-three|", b"op-four"]
    expected = b"".join(chunks)

    async with client_as(USER_A) as c:
        drive = (
            await c.post(
                "/drives",
                json={"title": "Compaction test drive", "keyNonce": "aa", "counterNonce": "bb",
                      "customConfig": _custom(master_bucket)},
            )
        ).json()
        drive_id = drive["id"]
        base = f"/drives/{drive_id}/storage-targets"

        # Empty drive -> the slave is ACTIVE right away and takes part in every write below.
        resp = await c.post(base, json={"customConfig": _custom(slave_bucket)})
        assert resp.status_code == 201 and resp.json()["status"] == "ACTIVE"

        for chunk in chunks:
            assert (await c.post(f"/ops/{drive_id}", content=chunk)).status_code == 204

        # Added after the writes, so it is never written to; it only has to be
        # skipped by compaction, not to block it.
        resp = await c.post(base, json={"customConfig": _custom("whatever", "http://localhost:1")})
        assert resp.status_code == 201
        dead_target_id = resp.json()["id"]

    # Fan-out wrote its own copy of every chunk onto both live targets.
    for bucket in (master_bucket, slave_bucket):
        assert len(await _minio_list_keys(bucket, f"ops/{drive_id}/")) == len(chunks)

    async with async_session_factory() as db:
        results = await OpsCompactionService().compact_all_drives(db)

    ours = [r for r in results if r["drive_id"] == drive_id]
    assert len(ours) == 2  # master + the reachable slave; the dead one was skipped, not fatal
    assert dead_target_id not in {r["target_id"] for r in ours}
    assert all(r["batched_chunks"] == len(chunks) for r in ours)

    # Each target was compacted on its own: no loose chunks left, one batch each.
    for bucket in (master_bucket, slave_bucket):
        assert await _minio_list_keys(bucket, f"ops/{drive_id}/") == []
        assert len(await _minio_list_keys(bucket, f"ops-batches/{drive_id}/")) == 1

    # Recovery reads the log back from the master, and the copy on the slave is identical.
    async with async_session_factory() as db:
        targets = await get_active_targets(db, uuid.UUID(drive_id))
    master = get_master(targets)
    assert master.bucket == master_bucket
    assert await reconstruct_ops_from_backup(master, drive_id) == expected
    slave = next(t for t in targets if t.bucket == slave_bucket)
    assert await reconstruct_ops_from_backup(slave, drive_id) == expected

    # A second pass has nothing left to merge.
    async with async_session_factory() as db:
        again = [r for r in await OpsCompactionService().compact_all_drives(db) if r["drive_id"] == drive_id]
    assert [r["batched_chunks"] for r in again] == [0, 0]


# --- epic 0021: a failed write names the storage that failed, and only that ---

DEAD_ENDPOINT = "http://localhost:1"


def _custom_config(bucket: str, endpoint: str = MINIO_ENDPOINT) -> dict:
    return {
        "endpointUrl": endpoint, "region": "us-east-1", "bucket": bucket,
        "accessKey": MINIO_ACCESS_KEY, "secretKey": MINIO_SECRET_KEY, "useSsl": False,
    }


def _assert_storage_error(resp, failed: dict[str, str], forbidden: list[str], rolled_back: bool = True) -> None:
    """`failed` maps targetId -> expected role. `forbidden` are strings that
    must not appear anywhere in the response (endpoints, buckets, credentials)."""
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "STORAGE_TARGETS_UNAVAILABLE"
    assert isinstance(detail["message"], str) and detail["message"]
    assert detail["rolledBack"] is rolled_back
    assert {t["targetId"]: t["role"] for t in detail["targets"]} == failed
    for t in detail["targets"]:
        assert set(t) == {"targetId", "role", "reason"}  # no name, no storage parameter
        assert t["reason"] == "UNREACHABLE"
    for text in forbidden:
        assert text not in resp.text, f"{text!r} leaked into the error response"


async def test_write_endpoints_name_the_failed_storage_and_leave_nothing_behind(client_as):
    master_bucket = f"test-wrerr-master-{uuid.uuid4().hex[:12]}"
    slave_bucket = f"test-wrerr-slave-{uuid.uuid4().hex[:12]}"
    dead_bucket = f"test-wrerr-dead-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(master_bucket)
    await _ensure_minio_bucket(slave_bucket)
    forbidden = ["localhost:1", dead_bucket, master_bucket, slave_bucket, MINIO_ACCESS_KEY, "Traceback"]

    async with client_as(USER_A) as c:
        drive_id = (
            await c.post(
                "/drives",
                json={"title": "Write-error test drive", "keyNonce": "aa", "counterNonce": "bb",
                      "customConfig": _custom_config(master_bucket)},
            )
        ).json()["id"]
        base = f"/drives/{drive_id}/storage-targets"
        # An empty drive: both new slaves are ACTIVE at once and take part in every write.
        assert (await c.post(base, json={"customConfig": _custom_config(slave_bucket)})).json()["status"] == "ACTIVE"
        dead = (await c.post(base, json={"customConfig": _custom_config(dead_bucket, DEAD_ENDPOINT)})).json()
        assert dead["status"] == "ACTIVE"

        # POST /ops: used to be a bare 500.
        resp = await c.post(f"/ops/{drive_id}", content=b"\x01\x02\x03")
        _assert_storage_error(resp, {dead["id"]: "SLAVE"}, forbidden)
        # Rolled back everywhere: Postgres did not grow, the healthy storages hold no chunk.
        assert (await c.get(f"/ops/{drive_id}/0")).content == b""
        for bucket in (master_bucket, slave_bucket):
            assert await _minio_list_keys(bucket, f"ops/{drive_id}/") == []

        # POST /data: used to be an unreadable string built from the raw exceptions.
        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 10})).json()
        resp = await c.post("/data", params={"sessionId": session["sessionId"], "start": 0}, content=b"0123456789")
        _assert_storage_error(resp, {dead["id"]: "SLAVE"}, forbidden)
        for bucket in (master_bucket, slave_bucket):
            assert await _minio_list_keys(bucket, f"data/{drive_id}/") == []


async def test_write_endpoints_blame_the_master_when_it_is_the_unavailable_one(client_as):
    """A partial-block write first reads the block from the master; if the
    master is down that read fails. It must be reported as the master's
    failure (not a bare 500), and POST /ops must name it too."""
    good_bucket = f"test-wrerr-good-{uuid.uuid4().hex[:12]}"
    dead_bucket = f"test-wrerr-deadmaster-{uuid.uuid4().hex[:12]}"
    await _ensure_minio_bucket(good_bucket)
    forbidden = ["localhost:1", dead_bucket, good_bucket, MINIO_ACCESS_KEY, "Traceback"]

    async with client_as(USER_A) as c:
        drive_id = (
            await c.post(
                "/drives",
                json={"title": "Dead-master test drive", "keyNonce": "aa", "counterNonce": "bb",
                      "customConfig": _custom_config(good_bucket)},
            )
        ).json()["id"]
        base = f"/drives/{drive_id}/storage-targets"
        dead = (await c.post(base, json={"customConfig": _custom_config(dead_bucket, DEAD_ENDPOINT)})).json()
        assert (await c.patch(f"{base}/{dead['id']}/make-master")).status_code == 204

        session = (await c.post("/sessions", params={"driveId": drive_id, "byteLength": 10})).json()
        resp = await c.post("/data", params={"sessionId": session["sessionId"], "start": 0}, content=b"0123456789")
        _assert_storage_error(resp, {dead["id"]: "MASTER"}, forbidden)

        resp = await c.post(f"/ops/{drive_id}", content=b"\x01")
        _assert_storage_error(resp, {dead["id"]: "MASTER"}, forbidden)
        assert await _minio_list_keys(good_bucket, f"data/{drive_id}/") == []
        assert await _minio_list_keys(good_bucket, f"ops/{drive_id}/") == []


async def test_creating_a_drive_on_an_unavailable_storage_explains_why_and_creates_nothing(client_as):
    title = f"Dead-storage drive {uuid.uuid4().hex[:8]}"
    dead_bucket = f"test-wrerr-create-{uuid.uuid4().hex[:12]}"

    async with client_as(USER_A) as c:
        resp = await c.post(
            "/drives",
            json={"title": title, "keyNonce": "aa", "counterNonce": "bb",
                  "customConfig": _custom_config(dead_bucket, DEAD_ENDPOINT)},
        )
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "STORAGE_TARGETS_UNAVAILABLE"
        assert [(t["role"], t["reason"]) for t in detail["targets"]] == [("MASTER", "UNREACHABLE")]
        for text in ("localhost:1", dead_bucket, MINIO_ACCESS_KEY):
            assert text not in resp.text

        # The failed creation was rolled back: the drive does not exist.
        assert title not in [d["title"] for d in (await c.get("/drives")).json()]
