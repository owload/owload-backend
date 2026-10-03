"""Unit tests for BlockStorage's fan-out write/rollback and free_range's
partial-failure handling. Uses fake S3 clients (no real S3/MinIO needed)
since forcing a real provider to fail a specific target on demand isn't
practical.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

from app.services.storage import BlockDeleteError, BlockStorage, BlockWriteError
from app.services.storage_targets import ResolvedTarget

pytestmark = pytest.mark.asyncio


def _make_target(role: str = "MASTER", bucket: str = "test-bucket") -> ResolvedTarget:
    return ResolvedTarget(
        id=uuid.uuid4(),
        role=role,
        endpoint_url=None,
        region="us-east-1",
        bucket=bucket,
        access_key="ak",
        secret_key="sk",
        use_ssl=True,
    )


class _NoSuchKey(Exception):
    pass


class _FakeClient:
    """Minimal fake of an aioboto3 S3 client. get_object always reports the
    key as missing (empty block) unless a subclass overrides it — enough for
    write_range's read-modify-write step against small writes."""

    exceptions = type("Exceptions", (), {"NoSuchKey": _NoSuchKey})

    def __init__(self, delete_objects_responses: list[dict] | None = None) -> None:
        self._delete_objects_responses = list(delete_objects_responses or [])
        self.calls: list[dict] = []

    async def get_object(self, Bucket, Key, Range=None):
        self.calls.append({"op": "get_object", "bucket": Bucket, "key": Key, "range": Range})
        raise self.exceptions.NoSuchKey()

    async def put_object(self, Bucket, Key, Body, **kwargs):
        self.calls.append({"op": "put_object", "bucket": Bucket, "key": Key})
        return {}

    async def delete_object(self, Bucket, Key):
        self.calls.append({"op": "delete_object", "bucket": Bucket, "key": Key})
        return {}

    async def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.calls.append({"op": "delete_objects", "bucket": Bucket, "keys": keys})
        return self._delete_objects_responses.pop(0)


class _FailingPutClient(_FakeClient):
    async def put_object(self, Bucket, Key, Body, **kwargs):
        raise RuntimeError("simulated PUT failure")


def _storage_with_fake_clients(clients_by_target_id: dict[uuid.UUID, _FakeClient]) -> BlockStorage:
    storage = BlockStorage()

    @asynccontextmanager
    async def fake_ctx(target: ResolvedTarget):
        yield clients_by_target_id[target.id]

    storage._client_ctx = fake_ctx  # type: ignore[method-assign]
    return storage


# --- free_range: unchanged single-target behavior, now via a target list ---


async def test_free_range_succeeds_when_delete_objects_reports_no_errors():
    from app.services.storage import BLOCK_SIZE

    target = _make_target()
    fake_client = _FakeClient([{"Deleted": [{"Key": "k1"}, {"Key": "k2"}], "Errors": []}])
    storage = _storage_with_fake_clients({target.id: fake_client})

    deleted = await storage.free_range([target], uuid.uuid4(), 0, 2 * BLOCK_SIZE)

    assert deleted == 2
    assert len(fake_client.calls) == 1


async def test_free_range_raises_when_delete_objects_reports_errors():
    from app.services.storage import BLOCK_SIZE

    target = _make_target()
    fake_client = _FakeClient(
        [
            {
                "Deleted": [{"Key": "data/x/000000000000"}],
                "Errors": [{"Key": "data/x/000000000001", "Code": "InternalError", "Message": "boom"}],
            }
        ]
    )
    storage = _storage_with_fake_clients({target.id: fake_client})

    with pytest.raises(BlockDeleteError) as exc_info:
        await storage.free_range([target], uuid.uuid4(), 0, 2 * BLOCK_SIZE)

    assert len(exc_info.value.errors) == 1
    assert exc_info.value.errors[0]["Key"] == "data/x/000000000001"


async def test_free_range_collects_errors_across_multiple_batches():
    """A range spanning >1000 blocks issues multiple delete_objects calls;
    an error in any of them must still be reported, not swallowed because a
    later batch succeeded."""
    from app.services.storage import BLOCK_SIZE

    target = _make_target()
    n_blocks = 1001  # forces exactly 2 batches (1000 + 1)
    fake_client = _FakeClient(
        [
            {"Deleted": [], "Errors": [{"Key": "data/x/000000000005", "Code": "AccessDenied", "Message": "nope"}]},
            {"Deleted": [{"Key": "data/x/000000001000"}], "Errors": []},
        ]
    )
    storage = _storage_with_fake_clients({target.id: fake_client})

    with pytest.raises(BlockDeleteError) as exc_info:
        await storage.free_range([target], uuid.uuid4(), 0, n_blocks * BLOCK_SIZE)

    assert len(fake_client.calls) == 2
    assert len(exc_info.value.errors) == 1
    assert exc_info.value.errors[0]["Key"] == "data/x/000000000005"


async def test_free_range_fans_out_to_every_target():
    from app.services.storage import BLOCK_SIZE

    master, slave = _make_target(role="MASTER"), _make_target(role="SLAVE")
    c_master = _FakeClient([{"Deleted": [{"Key": "k"}], "Errors": []}])
    c_slave = _FakeClient([{"Deleted": [{"Key": "k"}], "Errors": []}])
    storage = _storage_with_fake_clients({master.id: c_master, slave.id: c_slave})

    deleted = await storage.free_range([master, slave], uuid.uuid4(), 0, BLOCK_SIZE)

    assert deleted == 1
    assert len(c_master.calls) == 1
    assert len(c_slave.calls) == 1


# --- write_range: fan-out to all targets, compensating rollback on partial failure ---


async def test_write_range_single_target_no_regression():
    target = _make_target()
    client = _FakeClient()
    storage = _storage_with_fake_clients({target.id: client})

    await storage.write_range([target], uuid.uuid4(), 0, b"hello")

    assert [c["op"] for c in client.calls if c["op"] != "get_object"] == ["put_object"]


async def test_write_range_fans_out_to_all_targets():
    master, slave = _make_target(role="MASTER"), _make_target(role="SLAVE")
    c_master, c_slave = _FakeClient(), _FakeClient()
    storage = _storage_with_fake_clients({master.id: c_master, slave.id: c_slave})

    await storage.write_range([master, slave], uuid.uuid4(), 0, b"x" * 10)

    assert any(c["op"] == "put_object" for c in c_master.calls)
    assert any(c["op"] == "put_object" for c in c_slave.calls)


async def test_write_range_rolls_back_succeeded_targets_on_partial_failure():
    master = _make_target(role="MASTER")
    slave_ok = _make_target(role="SLAVE")
    slave_fail = _make_target(role="SLAVE")

    c_master, c_ok, c_fail = _FakeClient(), _FakeClient(), _FailingPutClient()
    storage = _storage_with_fake_clients({master.id: c_master, slave_ok.id: c_ok, slave_fail.id: c_fail})

    with pytest.raises(BlockWriteError) as exc_info:
        await storage.write_range([master, slave_ok, slave_fail], uuid.uuid4(), 0, b"x" * 10)

    assert slave_fail.id in exc_info.value.errors

    # The two targets whose PUT succeeded get a compensating DELETE of the same key.
    assert any(c["op"] == "delete_object" for c in c_master.calls)
    assert any(c["op"] == "delete_object" for c in c_ok.calls)
    # The failing target's own PUT never succeeded, so there's nothing to roll back on it.
    assert not any(c["op"] == "delete_object" for c in c_fail.calls)


async def test_write_range_no_target_written_when_first_block_of_multi_block_write_fails():
    """A write spanning multiple blocks that fails partway must roll back
    every block already committed on every target, not just the failing one."""
    from app.services.storage import BLOCK_SIZE

    master = _make_target(role="MASTER")
    slave = _make_target(role="SLAVE")

    class FailOnSecondBlock(_FakeClient):
        def __init__(self):
            super().__init__()
            self._put_count = 0

        async def put_object(self, Bucket, Key, Body, **kwargs):
            self._put_count += 1
            if self._put_count == 2:
                raise RuntimeError("simulated failure on second block")
            return await super().put_object(Bucket, Key, Body, **kwargs)

    c_master, c_slave = _FakeClient(), FailOnSecondBlock()
    storage = _storage_with_fake_clients({master.id: c_master, slave.id: c_slave})

    # Two full blocks -> two separate fan-out PUT rounds.
    data = b"a" * BLOCK_SIZE + b"b" * BLOCK_SIZE
    with pytest.raises(BlockWriteError):
        await storage.write_range([master, slave], uuid.uuid4(), 0, data)

    # Block 0 succeeded on both targets, then block 1 failed on the slave —
    # master's PUTs for *both* blocks must be rolled back, not just the one
    # that raced with the failure; the slave only ever succeeded on block 0.
    master_deletes = {c["key"] for c in c_master.calls if c["op"] == "delete_object"}
    slave_deletes = {c["key"] for c in c_slave.calls if c["op"] == "delete_object"}
    assert len(master_deletes) == 2
    assert len(slave_deletes) == 1


# --- test_connection: epic 0012 task 0009 ---


class _HeadBucketClient(_FakeClient):
    async def head_bucket(self, Bucket):
        self.calls.append({"op": "head_bucket", "bucket": Bucket})
        return {}


class _FailingHeadBucketClient(_FakeClient):
    async def head_bucket(self, Bucket):
        raise RuntimeError("bucket not found")


class _HangingHeadBucketClient(_FakeClient):
    async def head_bucket(self, Bucket):
        await asyncio.sleep(10)


async def test_test_connection_reports_ok_on_successful_head_bucket():
    target = _make_target()
    storage = _storage_with_fake_clients({target.id: _HeadBucketClient()})

    ok, error = await storage.test_connection(target)

    assert ok is True
    assert error is None


async def test_test_connection_reports_error_message_on_failure():
    target = _make_target()
    storage = _storage_with_fake_clients({target.id: _FailingHeadBucketClient()})

    ok, error = await storage.test_connection(target)

    assert ok is False
    assert "bucket not found" in error


async def test_test_connection_times_out_instead_of_hanging(monkeypatch):
    import app.services.storage as storage_module

    monkeypatch.setattr(storage_module, "CONNECTION_TEST_TIMEOUT_SECONDS", 0.05)
    target = _make_target()
    storage = _storage_with_fake_clients({target.id: _HangingHeadBucketClient()})

    ok, error = await storage.test_connection(target)

    assert ok is False
    assert "timed out" in error


# --- failure classification and the client-safe view of a failed write (epic 0021) ---

from botocore.exceptions import (  # noqa: E402
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

from app.services.storage_errors import StorageFailureReason, classify_storage_error  # noqa: E402

# Strings that must never reach a client: they are what the raw errors contain.
_SECRET_HOST = "s3.secret-host.example"
_SECRET_BUCKET = "private-bucket-name"
_SECRET_KEY_ID = "fake-access-key-id-for-tests"


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{_SECRET_BUCKET} {_SECRET_KEY_ID}"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )


@pytest.mark.parametrize(
    "exc, expected",
    [
        (EndpointConnectionError(endpoint_url=f"https://{_SECRET_HOST}/{_SECRET_BUCKET}"), StorageFailureReason.UNREACHABLE),
        (ConnectTimeoutError(endpoint_url=f"https://{_SECRET_HOST}"), StorageFailureReason.UNREACHABLE),
        (ReadTimeoutError(endpoint_url=f"https://{_SECRET_HOST}"), StorageFailureReason.UNREACHABLE),
        (TimeoutError("timed out"), StorageFailureReason.UNREACHABLE),
        (ConnectionResetError("reset by peer"), StorageFailureReason.UNREACHABLE),
        (_client_error("ServiceUnavailable", 503), StorageFailureReason.UNREACHABLE),
        (_client_error("SlowDown", 503), StorageFailureReason.UNREACHABLE),
        (_client_error("InternalError", 500), StorageFailureReason.UNREACHABLE),
        (_client_error("AccessDenied", 403), StorageFailureReason.ACCESS_DENIED),
        (_client_error("InvalidAccessKeyId", 403), StorageFailureReason.ACCESS_DENIED),
        (_client_error("SignatureDoesNotMatch", 403), StorageFailureReason.ACCESS_DENIED),
        (_client_error("ExpiredToken", 400), StorageFailureReason.ACCESS_DENIED),
        (_client_error("SomethingElse", 403), StorageFailureReason.ACCESS_DENIED),  # decided by HTTP status
        (NoCredentialsError(), StorageFailureReason.ACCESS_DENIED),
        (_client_error("NoSuchBucket", 404), StorageFailureReason.BUCKET_NOT_FOUND),
        (_client_error("EntityTooLarge", 400), StorageFailureReason.OTHER),
        (RuntimeError("anything else"), StorageFailureReason.OTHER),
        (ValueError("not an S3 error at all"), StorageFailureReason.OTHER),
    ],
)
async def test_classify_storage_error(exc, expected):
    assert classify_storage_error(exc) == expected


async def test_block_write_error_exposes_only_safe_per_target_failures():
    master = _make_target(role="MASTER")
    slave = _make_target(role="SLAVE")

    class _Raising(_FakeClient):
        def __init__(self, exc):
            super().__init__()
            self._exc = exc

        async def put_object(self, Bucket, Key, Body, **kwargs):
            raise self._exc

    storage = _storage_with_fake_clients(
        {
            master.id: _FakeClient(),
            slave.id: _Raising(EndpointConnectionError(endpoint_url=f"https://{_SECRET_HOST}/{_SECRET_BUCKET}")),
        }
    )

    with pytest.raises(BlockWriteError) as exc_info:
        await storage.write_range([master, slave], uuid.uuid4(), 0, b"x" * 10)

    err = exc_info.value
    assert [(f.target_id, f.role, f.reason) for f in err.failures] == [
        (slave.id, "SLAVE", StorageFailureReason.UNREACHABLE)
    ]
    assert not hasattr(err.failures[0], "label")  # no storage name is carried at all
    assert err.rolled_back is True  # the master's successful PUT was deleted again
    # The raw error is kept for the server log, but nothing derived from it for the client leaks it.
    assert slave.id in err.errors
    exposed = repr(err.failures) + str(err)
    assert _SECRET_HOST not in exposed and _SECRET_BUCKET not in exposed


async def test_block_write_error_reports_a_failed_rollback():
    master, slave = _make_target(role="MASTER"), _make_target(role="SLAVE")

    class _CannotRollBack(_FakeClient):
        async def delete_object(self, Bucket, Key):
            raise RuntimeError("delete failed too")

    storage = _storage_with_fake_clients({master.id: _CannotRollBack(), slave.id: _FailingPutClient()})

    with pytest.raises(BlockWriteError) as exc_info:
        await storage.write_range([master, slave], uuid.uuid4(), 0, b"x" * 10)

    assert exc_info.value.rolled_back is False  # part of the write may remain on the master


async def test_failed_master_read_is_reported_as_a_master_write_failure_and_rolls_back():
    """A partial-block write first reads the block from the master. If that
    read fails, it is the master that is to blame, and any blocks of the same
    call that were already written must be undone."""
    from app.services.storage import BLOCK_SIZE

    master, slave = _make_target(role="MASTER"), _make_target(role="SLAVE")

    class _MasterReadFails(_FakeClient):
        async def get_object(self, Bucket, Key, Range=None):
            raise EndpointConnectionError(endpoint_url=f"https://{_SECRET_HOST}")

    c_master, c_slave = _MasterReadFails(), _FakeClient()
    storage = _storage_with_fake_clients({master.id: c_master, slave.id: c_slave})

    # Block 0 is a full block (written without any read); block 1 is partial and needs the master read.
    data = b"a" * BLOCK_SIZE + b"b" * 10
    with pytest.raises(BlockWriteError) as exc_info:
        await storage.write_range([master, slave], uuid.uuid4(), 0, data)

    err = exc_info.value
    assert [(f.target_id, f.role, f.reason) for f in err.failures] == [
        (master.id, "MASTER", StorageFailureReason.UNREACHABLE)
    ]
    assert err.rolled_back is True
    # Block 0 had been written on both targets; both copies are deleted again.
    assert sum(c["op"] == "delete_object" for c in c_master.calls) == 1
    assert sum(c["op"] == "delete_object" for c in c_slave.calls) == 1
