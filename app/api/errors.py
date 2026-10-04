"""Error responses shared by the routes."""

from fastapi import HTTPException, status

from app.services.storage import BlockWriteError

STORAGE_TARGETS_UNAVAILABLE = "STORAGE_TARGETS_UNAVAILABLE"


def storage_write_failed(exc: BlockWriteError) -> HTTPException:
    """502 for a fan-out write that failed on one or more storage targets
    (owload-docs/epics/0021). The body names the failed targets and the reason
    in a small fixed vocabulary, and says whether the write was fully undone.

    It names no storage and exposes no parameter of one: only the target's id and
    role (MASTER or SLAVE), which GET /drives/{driveId}/storage-targets already
    shows to the same user, and the reason. Never an endpoint, bucket, object
    key, credential, a storage's name or the raw exception text; those stay in
    the server log."""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": STORAGE_TARGETS_UNAVAILABLE,
            "message": "The data could not be saved because a storage of this drive is not available.",
            "targets": [
                {
                    "targetId": str(failure.target_id),
                    "role": failure.role,
                    "reason": failure.reason,
                }
                for failure in exc.failures
            ],
            "rolledBack": exc.rolled_back,
        },
    )


STORAGE_CONNECTION_FAILED = "STORAGE_CONNECTION_FAILED"

_CONNECTION_FAILED_MESSAGES = {
    "UNREACHABLE": "The storage is not reachable or is not responding.",
    "ACCESS_DENIED": "The storage refused the access key and secret key.",
    "BUCKET_NOT_FOUND": "The bucket was not found on this storage.",
}


def storage_connection_failed(reason: str) -> HTTPException:
    """502 for a storage the caller supplied the connection for (restoring drives,
    owload-docs/epics/0022) that could not be read. Same fixed reasons as the write
    errors; the endpoint, bucket and the raw exception are never echoed back."""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": STORAGE_CONNECTION_FAILED,
            "message": _CONNECTION_FAILED_MESSAGES.get(reason, "The storage could not be read."),
            "reason": reason,
        },
    )
