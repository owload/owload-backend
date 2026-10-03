"""Describes a failed S3 call in a small, safe vocabulary.

A write that fails on one of a drive's storage targets (owload-docs/epics/0021)
has to tell the user *which* storage failed and *why*, without handing the raw
low-level error to the client: botocore/aiohttp messages routinely contain the
endpoint URL, the bucket and object keys. This module maps an exception to one
of a few reasons; the raw exception stays on the server, in the logs.
"""

import uuid
from dataclasses import dataclass

import aiohttp
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ProxyConnectionError,
    ReadTimeoutError,
    SSLError,
)


class StorageFailureReason:
    # Cannot connect, the connection dropped, a timeout, or the storage itself
    # answering with a server-side failure or throttling.
    UNREACHABLE = "UNREACHABLE"
    # The credentials are wrong, expired, or not allowed to write.
    ACCESS_DENIED = "ACCESS_DENIED"
    BUCKET_NOT_FOUND = "BUCKET_NOT_FOUND"
    OTHER = "OTHER"


@dataclass(frozen=True)
class TargetFailure:
    """One failed target of a fan-out write, in a form that is safe to show to
    the drive's users: only what GET /drives/{id}/storage-targets exposes
    already (id, role, a display label) plus the classified reason."""

    target_id: uuid.UUID
    role: str
    label: str
    reason: str


_ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AllAccessDisabled",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "ExpiredToken",
    "InvalidToken",
    "TokenRefreshRequired",
    "AccountProblem",
    "InvalidSecurity",
    "Forbidden",
    "Unauthorized",
}
_UNAVAILABLE_CODES = {
    "InternalError",
    "ServiceUnavailable",
    "SlowDown",
    "RequestTimeout",
    "RequestTimeTooSkewed",
    "ServerBusy",
    "Throttling",
    "ThrottlingException",
    "RequestLimitExceeded",
}
_CONNECTION_ERRORS = (
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
    ConnectionClosedError,
    ProxyConnectionError,
    SSLError,
    aiohttp.ClientError,
    ConnectionError,
    TimeoutError,
)


def classify_storage_error(exc: BaseException) -> str:
    """Maps the exception of a failed S3 call to a StorageFailureReason."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == "NoSuchBucket":
            return StorageFailureReason.BUCKET_NOT_FOUND
        if code in _ACCESS_DENIED_CODES or status in (401, 403):
            return StorageFailureReason.ACCESS_DENIED
        if code in _UNAVAILABLE_CODES or (isinstance(status, int) and (status >= 500 or status in (408, 429))):
            return StorageFailureReason.UNREACHABLE
        return StorageFailureReason.OTHER
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return StorageFailureReason.ACCESS_DENIED
    if isinstance(exc, _CONNECTION_ERRORS):
        return StorageFailureReason.UNREACHABLE
    if isinstance(exc, BotoCoreError):
        return StorageFailureReason.OTHER
    return StorageFailureReason.OTHER
