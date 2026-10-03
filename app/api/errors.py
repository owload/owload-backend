"""Error responses shared by the routes."""

from fastapi import HTTPException, status

from app.services.storage import BlockWriteError

STORAGE_TARGETS_UNAVAILABLE = "STORAGE_TARGETS_UNAVAILABLE"


def storage_write_failed(exc: BlockWriteError) -> HTTPException:
    """502 for a fan-out write that failed on one or more storage targets
    (owload-docs/epics/0021). The body names the failed targets and the reason
    in a small fixed vocabulary, and says whether the write was fully undone.

    It carries only what GET /drives/{driveId}/storage-targets already shows to
    the same user (id, role, a display label) — never an endpoint, bucket,
    object key, credential or the raw exception text; those stay in the server
    log."""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": STORAGE_TARGETS_UNAVAILABLE,
            "message": "The data could not be saved because a storage of this drive is not available.",
            "targets": [
                {
                    "targetId": str(failure.target_id),
                    "role": failure.role,
                    "label": failure.label,
                    "reason": failure.reason,
                }
                for failure in exc.failures
            ],
            "rolledBack": exc.rolled_back,
        },
    )
