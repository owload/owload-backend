import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_accessible_drive_or_404
from app.api.errors import storage_write_failed
from app.core.security import CurrentUser, get_current_user
from app.db.models import Drive, DriveAction
from app.db.session import get_db
from app.services.access import Role
from app.services.activity import (
    DRIVE_OPENED,
    DRIVE_WRITTEN,
    ClientContext,
    get_client_context,
    note_drive_activity,
)
from app.services.audit import log_action
from app.services.storage import BlockStorage, BlockWriteError, get_storage
from app.services.storage_targets import get_active_targets

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/{drive_id}/{start_byte_pos}")
async def get_operations(
    drive_id: uuid.UUID,
    start_byte_pos: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ctx: ClientContext = Depends(get_client_context),
) -> Response:
    if start_byte_pos < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "startBytePos must not be negative")
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub)
    data = bytes(drive.ops_data[start_byte_pos:])
    await note_drive_activity(db, user, drive_id, drive.title, DRIVE_OPENED, ctx, commit=True)

    return Response(content=data, media_type="application/octet-stream")


@router.post("/{drive_id}", status_code=status.HTTP_204_NO_CONTENT)
async def save_operation(
    drive_id: uuid.UUID,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: BlockStorage = Depends(get_storage),
    ctx: ClientContext = Depends(get_client_context),
) -> None:
    # Invariant relied on by the frontend: owload-front's DriveLogs correlates
    # this call's SAVE_OP audit-log entry with an FsOperationWrapper by matching
    # byte offsets (attributes.start + OPS_SEPARATOR_BYTE_LENGTH === op.startBytePos),
    # which only works because SerializingOpsRepository/SplittingOpsRepository
    # send exactly one operation per POST /ops call today. If that ever becomes
    # many-operations-per-call (e.g. batching for fewer round trips), the
    # correlation formula on the frontend breaks silently — coordinate any
    # change here with owload-docs/epics/0004-drive-logs-technical-debt/tasks/0003.
    drive = await get_accessible_drive_or_404(db, drive_id, user.sub, Role.WRITER)
    targets = await get_active_targets(db, drive_id)
    chunk = await request.body()

    result = await db.execute(
        update(Drive)
        .where(Drive.id == drive_id)
        .values(ops_data=Drive.ops_data.op("||")(chunk))
        .returning(func.length(Drive.ops_data))
    )
    new_length = result.scalar_one()
    start = new_length - len(chunk)

    # Dual-write to S3 before commit: if the backup write fails, the
    # exception propagates and the uncommitted Postgres append is rolled
    # back when the session closes, so ops_data and its S3 backup never
    # diverge (no chunk is "acked" to Postgres without a matching backup).
    # Fanned out to every active target (owload-docs/decisions/0011).
    try:
        await storage.append_ops_backup(targets, drive_id, chunk, start, new_length)
    except BlockWriteError as exc:
        raise storage_write_failed(exc) from exc

    log_action(db, drive_id, user.sub, DriveAction.SAVE_OP, start=start, end=new_length)
    await note_drive_activity(db, user, drive_id, drive.title, DRIVE_WRITTEN, ctx)
    await db.commit()
